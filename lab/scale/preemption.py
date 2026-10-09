#!/usr/bin/env python3
"""Which pod does the real kube-scheduler preempt for a controller, with and without the budget
on running builds? Run against a kwok cluster (a real scheduler, nodes that pretend).

Two machines, each nearly full with one sandbox of priority -10: idle on one, running a build
(label netci.io/busy) on the other. The busy one is created last: with everything else equal,
the scheduler prefers the machine whose victim started latest -- so without the budget it takes
the running build. Then a controller of priority 900000 that fits only by preempting arrives.

    KUBECONFIG=kwok.kubeconfig python3 lab/scale/preemption.py --rounds 5
"""
from __future__ import annotations

import argparse
import datetime
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NS = "preemption"


def kubectl(*args: str, stdin: str | None = None, check: bool = True) -> str:
    out = subprocess.run(["kubectl", *args], input=stdin, capture_output=True, text=True, timeout=60)
    if check and out.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args)}: {out.stderr.strip()}")
    return out.stdout


def apply(obj: dict) -> None:
    kubectl("apply", "-f", "-", stdin=json.dumps(obj))


def node(name: str) -> dict:
    alloc = {"cpu": "4", "memory": "4Gi", "pods": "110"}
    return {"apiVersion": "v1", "kind": "Node", "metadata": {"name": name, "annotations": {"kwok.x-k8s.io/node": "fake"},
            "labels": {"kubernetes.io/hostname": name, "preemption": "true"}}, "status": {"allocatable": alloc, "capacity": alloc}}


def pod(name: str, node_name: str | None, mem: str, pclass: str, labels: dict) -> dict:
    spec = {"priorityClassName": pclass, "tolerations": [{"operator": "Exists"}],
            "nodeSelector": {"preemption": "true"},
            "containers": [{"name": "c", "image": "fake", "resources": {"requests": {"cpu": "100m", "memory": mem}}}]}
    if node_name:
        spec["nodeName"] = node_name
    return {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name, "namespace": NS, "labels": labels}, "spec": spec}


def running(name: str) -> bool:
    return kubectl("-n", NS, "get", "pod", name, "-o", "jsonpath={.status.phase}", check=False) == "Running"


def wait(what: str, ok, timeout: float = 60) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if ok():
            return
        time.sleep(0.5)
    raise TimeoutError(what)


def round_(with_budget: bool, k: int, only_builds: bool = False) -> dict:
    kubectl("delete", "namespace", NS, "--ignore-not-found", "--wait=true", "--timeout=120s")
    kubectl("create", "namespace", NS)
    if with_budget:
        apply({"apiVersion": "policy/v1", "kind": "PodDisruptionBudget", "metadata": {"name": "busy", "namespace": NS},
               "spec": {"minAvailable": 1000000, "selector": {"matchLabels": {"netci.io/busy": "true"}}}})
    # Alternate which machine holds the build, so that no tie-break by node name explains it.
    idle_node, busy_node = ("pre-a", "pre-b") if k % 2 == 0 else ("pre-b", "pre-a")
    idle_labels = {"netci.io/sandbox": "idle", **({"netci.io/busy": "true"} if only_builds else {})}
    apply(pod("idle", idle_node, "3Gi", "netci-sandbox", idle_labels))
    wait("idle running", lambda: running("idle"))
    time.sleep(1.5)  # the busy one starts later
    apply(pod("busy", busy_node, "3Gi", "netci-sandbox", {"netci.io/sandbox": "busy", "netci.io/busy": "true"}))
    wait("busy running", lambda: running("busy"))
    if with_budget:
        want = "2" if only_builds else "1"
        wait("budget sees the builds", lambda: kubectl("-n", NS, "get", "pdb", "busy", "-o", "jsonpath={.status.currentHealthy}") == want)
    apply(pod("controller", None, "2Gi", "netci-cell", {"app": "jenkins"}))
    wait("controller placed", lambda: kubectl("-n", NS, "get", "pod", "controller", "-o", "jsonpath={.spec.nodeName}") != "", 90)
    placed = kubectl("-n", NS, "get", "pod", "controller", "-o", "jsonpath={.spec.nodeName}")
    preempted = "busy (a running build)" if placed == busy_node else "idle" if placed == idle_node else placed
    if only_builds and placed:
        preempted = "a running build (the only room)"
    return {"budget": with_budget, "onlyBuilds": only_builds, "busyOn": busy_node, "controllerOn": placed, "preempted": preempted}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=5)
    args = ap.parse_args()
    for n in ("pre-a", "pre-b"):
        apply(node(n))
    for name, value in (("netci-sandbox", -10), ("netci-cell", 900000)):
        apply({"apiVersion": "scheduling.k8s.io/v1", "kind": "PriorityClass", "metadata": {"name": name}, "value": value,
               "preemptionPolicy": "Never" if value < 0 else "PreemptLowerPriority"})
    version = json.loads(kubectl("version", "-o", "json"))["serverVersion"]["gitVersion"]
    rounds = [round_(b, k) for b in (False, True) for k in range(args.rounds)]
    # Both machines run builds: the takeover must still happen, at a build's cost.
    rounds += [round_(True, k, only_builds=True) for k in range(2)]
    kubectl("delete", "namespace", NS, "--ignore-not-found", "--wait=false")
    without = [r for r in rounds if not r["budget"]]
    withb = [r for r in rounds if r["budget"] and not r["onlyBuilds"]]
    only = [r for r in rounds if r["onlyBuilds"]]
    facts = {"scenario": "preemption-with-running-build-budget", "kubernetes": version, "rounds": rounds,
             "withoutBudgetBuildsPreempted": sum(r["preempted"].startswith("busy") for r in without),
             "withBudgetBuildsPreempted": sum(r["preempted"].startswith("busy") for r in withb), "roundsEach": args.rounds,
             "onlyBuildsControllerPlaced": sum(bool(r["controllerOn"]) for r in only), "onlyBuildsRounds": len(only)}
    facts["verdict"] = "PASS" if facts["withBudgetBuildsPreempted"] == 0 and facts["onlyBuildsControllerPlaced"] == len(only) else "FAIL"
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = ROOT / "lab" / "evidence" / f"preemption-budget-{stamp}.json"
    out.write_text(json.dumps(facts, indent=2) + "\n")
    print(json.dumps({k: v for k, v in facts.items() if k != "rounds"}, indent=1))
    for r in rounds:
        print(r)
    print("evidence:", out.name)
    return 0 if facts["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
