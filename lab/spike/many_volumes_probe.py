#!/usr/bin/env python3
"""When a machine running many cells loses its power, do their volumes move as fast as one does?

The scale test (lab/scale) measures the supervisor at hundreds of cells, on nodes that pretend:
detection, fencing and the Lease. What it cannot measure is the storage: after fencing, every
cell of the lost machine needs its volume detached from the dead node and attached on another,
all at once. This probe does that on the lab, with real Longhorn volumes:

  * N cells (namespaces vol-01..vol-NN) from the real netci-cell chart -- cell agent, guard,
    Lease, volume on longhorn-sync -- with Jenkins replaced by lab/spike/standin, which answers
    the chart's probes and appends an fsynced sequence number to JENKINS_HOME five times a
    second. Jenkins' own start (~17 s) does not depend on how many volumes move.
  * All of them on one machine (the others tainted NoSchedule while they are placed), the
    supervisor's leader elsewhere.
  * The machine is powered off; the supervisor does the rest. Per cell, from the power loss: when
    the node was fenced, the pod replaced, scheduled, its volume attached on the new node, the
    Lease taken, the pod Ready. Then, read from each volume: which writes survived, and whether
    the sequence has a gap or a torn line.

Run it with --cells 1 and with --cells 8 (or more): the question is whether the storage's share
grows with the number of volumes.

    python3 lab/spike/many_volumes_probe.py --cells 8 [--runs 1] [--size 128Mi] [--node netci-lab-2] [--keep]

The volumes are small because the lab's Longhorn has little room to give: each disk schedules up
to (maximum - reserved) x over-provisioning, and the lab's three cells and PostgreSQL take 26 of
its 26.4 GiB. Size does not change an attach (a volume is sparse; attaching starts its engine
and connects its replicas), and the probe refuses to start when the volumes would not fit,
instead of waiting on one Longhorn cannot place ("precheck new replica failed: insufficient
storage").

Evidence: lab/evidence/many-volumes-<N>cells-<timestamp>.json. Without --keep, the cells are
uninstalled at the end (their volumes are deleted with them).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import probe  # noqa: E402  (KUBECONFIG, a kubectl that survives the loss of an API server)
from probe import kubectl, now, wait  # noqa: E402

ROOT = probe.ROOT
STANDIN = "172.17.0.1:8930/netci/lab-standin@sha256:ce842bb2f2236d7dee3504fb147b36eea23b430104dc0c1bea3c6d7b2efb8087"
CHART = ROOT / "deploy" / "helm" / "netci-cell"
VIRSH = ["virsh", "-c", "qemu:///system"]


def cell_ns(i: int) -> str:
    return f"vol-{i:02d}"


def netci_image() -> tuple[str, str]:
    """The netci image the lab's cells run, so the stand-in cells run the same cell agent."""
    image = kubectl("-n", "cell-b", "get", "statefulset", "jenkins", "-o",
                    "jsonpath={.spec.template.spec.initContainers[?(@.name=='cell-agent')].image}")
    repo, digest = image.split("@", 1)
    return repo, digest


def cell_pod(ns: str) -> dict | None:
    out = kubectl("-n", ns, "get", "pod", "jenkins-0", "-o", "json", check=False)
    return json.loads(out) if out.strip().startswith("{") else None


def ready(p: dict | None) -> bool:
    return bool(p) and any(c["type"] == "Ready" and c["status"] == "True" for c in p["status"].get("conditions") or [])


PLACEMENT_TAINT = "netci.io/probe-placement"


def gib(q: str) -> float:
    units = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30}
    for u, m in units.items():
        if q.endswith(u):
            return float(q[: -len(u)]) * m / 2**30
    return float(q) / 2**30


def room(new: int, size: str) -> None:
    """Refuse when Longhorn could not place a replica of each new volume on every machine (three
    replicas, three machines): what a disk may still schedule, and what must stay free on it."""
    if new == 0:
        return
    setting = lambda name: float(kubectl("-n", "longhorn-system", "get", "settings.longhorn.io", name, "-o", "jsonpath={.value}"))
    over, minimal = setting("storage-over-provisioning-percentage"), setting("storage-minimal-available-percentage")
    need = new * gib(size)
    for n in json.loads(kubectl("-n", "longhorn-system", "get", "nodes.longhorn.io", "-o", "json"))["items"]:
        for disk, st in (n["status"].get("diskStatus") or {}).items():
            spec = n["spec"]["disks"][disk]
            if not spec.get("allowScheduling"):
                continue
            G = 2**30
            schedulable = (st["storageMaximum"] - spec["storageReserved"]) * over / 100 / G - st["storageScheduled"] / G
            free = st["storageAvailable"] / G - st["storageMaximum"] * minimal / 100 / G
            if need > min(schedulable, free):
                raise RuntimeError(f"{n['metadata']['name']}: Longhorn can place {min(schedulable, free):.2f} GiB more, "
                                   f"{new} volumes of {size} need {need:.2f} GiB; use fewer cells or a smaller --size")


def place(cells: list[str], node: str, size: str) -> None:
    """Install the cells, or move them back, onto node: the other machines are tainted NoSchedule
    meanwhile. Not cordoned: Longhorn places no replica on a cordoned node
    (disable-scheduling-on-cordoned-node, true in the lab), so a volume created then could have
    one replica of three."""
    others = [n for n in probe.NODES if n != node]
    room(sum(kubectl("get", "namespace", ns, check=False) == "" for ns in cells), size)
    repo, digest = netci_image()
    pull = json.loads(kubectl("-n", "cell-b", "get", "secret", "harbor-pull", "-o", "json"))
    for n in others:
        kubectl("taint", "node", n, f"{PLACEMENT_TAINT}=many-volumes:NoSchedule", "--overwrite")
    try:
        for ns in cells:
            p = cell_pod(ns)
            if p and p["spec"].get("nodeName") == node and ready(p):
                continue
            # A namespace of a previous run may still be terminating: wait it out.
            wait(f"{ns} of a previous run gone", lambda ns=ns: "Terminating" not in kubectl(
                "get", "namespace", ns, "-o", "jsonpath={.status.phase}", check=False), 300, 2)
            if kubectl("get", "namespace", ns, check=False) == "":
                kubectl("create", "namespace", ns)
                secret = {"apiVersion": "v1", "kind": "Secret", "type": pull["type"], "data": pull["data"],
                          "metadata": {"name": "harbor-pull", "namespace": ns}}
                subprocess.run(["kubectl", "apply", "-f", "-"], input=json.dumps(secret), text=True, check=True,
                               capture_output=True)
                kubectl("-n", ns, "create", "configmap", "jenkins-casc", "--from-literal=jenkins.yaml={}")
                kubectl("-n", ns, "create", "secret", "generic", "jenkins-cell")
            if p:  # on another machine since a previous run: back onto this one
                kubectl("-n", ns, "delete", "pod", "jenkins-0", "--wait=true", "--timeout=120s")
            out = subprocess.run(["helm", "upgrade", "--install", ns, str(CHART), "-n", ns,
                            "-f", str(CHART / "ci" / "lab-values.yaml"),
                            "--set", f"image.controller={STANDIN}", "--set", f"netci.repository={repo}",
                            "--set", f"netci.digest={digest}", "--set", "storage.className=longhorn-sync",
                            "--set", f"storage.size={size}", "--set", "pluginsFromImageOnly=false",
                            "--set", "resources.requests.cpu=10m", "--set", "resources.requests.memory=16Mi",
                            "--set", "resources.limits.memory=64Mi", "--set", "terminationGracePeriodSeconds=10",
                            "--wait", "--timeout", "5m"], capture_output=True, text=True)
            if out.returncode != 0:
                raise RuntimeError(f"helm install {ns}: {out.stderr.strip()[-500:]}")
        for ns in cells:
            wait(f"{ns} Ready on {node}", lambda ns=ns: (lambda p: ready(p) and p["spec"]["nodeName"] == node)(cell_pod(ns)), 300, 2)
    finally:
        for n in others:
            kubectl("taint", "node", n, f"{PLACEMENT_TAINT}-", check=False)


def whole(cells: list[str], node: str) -> bool:
    for n in json.loads(kubectl("get", "nodes", "-o", "json"))["items"]:
        if not any(c["type"] == "Ready" and c["status"] == "True" for c in n["status"]["conditions"]):
            return False
        if any(t["key"] == "node.kubernetes.io/out-of-service" for t in n["spec"].get("taints") or []):
            return False
    robustness = kubectl("-n", "longhorn-system", "get", "volumes.longhorn.io", "-o",
                         "jsonpath={.items[*].status.robustness}", check=False).split()
    if not robustness or any(r != "healthy" for r in robustness):
        return False
    return all(ready(cell_pod(ns)) for ns in ("cell-a", "cell-b", *cells))


def leader_off(node: str) -> str:
    """The supervisor's leader on another machine: one lost with the machine adds its Lease's
    10 s to every takeover, which is ADR-060's case, not this probe's."""
    def leader() -> tuple[str, str]:
        holder = kubectl("-n", "netci-system", "get", "lease", "netci-supervisor", "-o", "jsonpath={.spec.holderIdentity}")
        where = kubectl("-n", "netci-system", "get", "pod", holder, "-o", "jsonpath={.spec.nodeName}", check=False)
        return holder, where
    holder, where = leader()
    if where == node:
        kubectl("-n", "netci-system", "delete", "pod", holder, "--wait=false")
        wait("a supervisor leader on another machine", lambda: (lambda h, w: h != holder and w and w != node)(*leader()), 120, 1)
        holder, where = leader()
    return where


def survived(ns: str, failure_epoch: float) -> dict:
    """Read back from the volume what the stand-in wrote: the sequence number it resumed from, how
    long before the power loss that write was made, and whether the file has a gap or torn line."""
    script = (
        'h=/var/jenkins_home; set -- $(tail -n 1 $h/starts); n=$2; '
        'last=$(awk -v n="$n" \'$1==n{print $2}\' $h/seq); '
        'bad=$(awk \'BEGIN{e=1;b=0} NF!=2||$1!=e{b++} {e=$1+1} END{print b}\' $h/seq); '
        'echo "$n ${last:-none} $bad $(grep -c . $h/starts)"')
    out = kubectl("-n", ns, "exec", "jenkins-0", "-c", "jenkins", "--", "sh", "-c", script).split()
    n, last, bad, starts = int(out[0]), out[1], int(out[2]), int(out[3])
    facts = {"resumedFromSeq": n, "badLines": bad, "starts": starts}
    if last != "none":
        facts["lastWriteBeforeFailureSeconds"] = round(failure_epoch - float(last), 3)
    return facts


def run_once(cells: list[str], node: str) -> dict:
    wait("the cluster whole, every cell on its machine", lambda: whole(cells, node), 1200, 5)
    facts: dict = {"node": node, "supervisorLeaderOn": leader_off(node)}
    time.sleep(20)  # leases, heartbeats and the supervisor's observations settle
    before, pvs = {}, {}
    for ns in cells:
        p = cell_pod(ns)
        if not p or p["spec"]["nodeName"] != node:
            raise RuntimeError(f"{ns} is not on {node}")
        before[ns] = p["metadata"]["uid"]
        pvs[ns] = kubectl("-n", ns, "get", "pvc", "home-jenkins-0", "-o", "jsonpath={.spec.volumeName}")
    holders = {ns: kubectl("-n", ns, "get", "lease", "jenkins", "-o", "jsonpath={.spec.holderIdentity}") for ns in cells}

    subprocess.run([*VIRSH, "destroy", node], check=True, capture_output=True)
    t0, failure_epoch = now(), time.time()
    facts["failureAt"] = dt.datetime.fromtimestamp(failure_epoch, dt.timezone.utc).isoformat(timespec="milliseconds")
    seen: dict[str, dict[str, float]] = {ns: {} for ns in cells}
    fenced = None

    def mark(ns: str, what: str) -> None:
        seen[ns].setdefault(what, round(now() - t0, 1))

    polls = 0
    while now() - t0 < 900:
        polls += 1
        if fenced is None and "out-of-service" in kubectl("get", "node", node, "-o", "jsonpath={.spec.taints[*].key}", check=False):
            fenced = round(now() - t0, 1)
        pods = {p["metadata"]["namespace"]: p for p in json.loads(
            kubectl("get", "pods", "-A", "-l", "netci.io/cell=true", "-o", "json", check=False) or '{"items":[]}')["items"]}
        attached = {(a["spec"]["source"].get("persistentVolumeName"), a["spec"]["nodeName"])
                    for a in json.loads(kubectl("get", "volumeattachments", "-o", "json", check=False) or '{"items":[]}')["items"]
                    if (a.get("status") or {}).get("attached")}
        leases = {l["metadata"]["namespace"]: l["spec"].get("holderIdentity") for l in json.loads(
            kubectl("get", "leases", "-A", "--field-selector", "metadata.name=jenkins", "-o", "json", check=False)
            or '{"items":[]}')["items"]}
        for ns in cells:
            p = pods.get(ns)
            if p is None or p["metadata"]["uid"] == before[ns]:
                continue
            mark(ns, "replaced")
            where = p["spec"].get("nodeName")
            if where:
                mark(ns, "scheduled")
                seen[ns]["on"] = where
                if (pvs[ns], where) in attached:
                    mark(ns, "attached")
            if leases.get(ns) and leases[ns] != holders[ns]:
                mark(ns, "leaseTaken")
            if ready(p):
                mark(ns, "ready")
        if all("ready" in seen[ns] for ns in cells):
            break
        time.sleep(0.5)
    facts["observedFor"] = round(now() - t0, 1)
    facts["polls"] = polls
    facts["fencedAfterSeconds"] = fenced
    for ns in cells:
        s = seen[ns]
        if "ready" in s:
            try:
                s.update(survived(ns, failure_epoch))
            except Exception as e:  # noqa: BLE001 - recorded; the verdict below fails the run
                s["readBackError"] = str(e)
        events = json.loads(kubectl("-n", ns, "get", "events", "--field-selector", "involvedObject.name=jenkins-0",
                                    "-o", "json", check=False) or '{"items":[]}')["items"]
        trouble = {}
        for e in events:
            # Events of a previous run stay for an hour: only this run's count.
            if (e.get("lastTimestamp") or e.get("eventTime") or "") < facts["failureAt"][:19]:
                continue
            if e.get("type") == "Warning" and e.get("reason") in ("FailedAttachVolume", "FailedMount", "FailedScheduling"):
                trouble[e["reason"]] = trouble.get(e["reason"], 0) + (e.get("count") or 1)
        if trouble:
            s["warnings"] = trouble
    facts["cells"] = seen

    # The machine back: the supervisor powers it on once nothing of a cell is left on it.
    try:
        wait("the supervisor powered the machine back on", lambda: subprocess.run(
            [*VIRSH, "domstate", node], capture_output=True, text=True).stdout.strip() == "running", 300, 2)
        facts["poweredBackOnBy"] = "supervisor"
    except TimeoutError:
        subprocess.run([*VIRSH, "start", node], check=False, capture_output=True)
        facts["poweredBackOnBy"] = "probe (the supervisor had not within 300 s)"
    return facts


def summary(runs: list[dict], cells: list[str]) -> dict:
    out: dict = {}
    for key in ("replaced", "scheduled", "attached", "leaseTaken", "ready"):
        values = [r["cells"][ns][key] for r in runs for ns in cells if key in r["cells"][ns]]
        if values:
            out[key] = {"median": statistics.median(values), "max": max(values), "n": len(values)}
    storage = [r["cells"][ns]["attached"] - r["cells"][ns]["scheduled"] for r in runs for ns in cells
               if "attached" in r["cells"][ns] and "scheduled" in r["cells"][ns]]
    if storage:
        out["attachAfterScheduled"] = {"median": round(statistics.median(storage), 1), "max": round(max(storage), 1)}
    lost = [r["cells"][ns]["lastWriteBeforeFailureSeconds"] for r in runs for ns in cells
            if "lastWriteBeforeFailureSeconds" in r["cells"][ns]]
    if lost:
        out["lastWriteBeforeFailureSeconds"] = {"min": min(lost), "max": max(lost)}
    out["fencedAfterSeconds"] = [r["fencedAfterSeconds"] for r in runs]
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cells", type=int, default=8)
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--node", default="netci-lab-2")
    ap.add_argument("--size", default="128Mi", help="each cell's volume")
    ap.add_argument("--keep", action="store_true", help="leave the cells installed")
    args = ap.parse_args()
    cells = [cell_ns(i) for i in range(1, args.cells + 1)]
    started = dt.datetime.now(dt.timezone.utc)
    facts: dict = {"scenario": "many-volumes-poweroff", "cells": args.cells, "storageClass": "longhorn-sync", "volumeSize": args.size,
                   "standin": STANDIN, "startedAt": started.isoformat(timespec="seconds"), "runs": []}
    try:
        for _ in range(args.runs):
            wait("the cluster whole", lambda: whole([], args.node), 1200, 5)
            place(cells, args.node, args.size)
            facts["runs"].append(run_once(cells, args.node))
        facts["summary"] = summary(facts["runs"], cells)
        ok = all("ready" in r["cells"][ns] and r["cells"][ns].get("badLines") == 0 and "readBackError" not in r["cells"][ns]
                 for r in facts["runs"] for ns in cells)
        facts["verdict"] = "PASS" if ok and facts["runs"] else "FAIL"
    except Exception as e:  # noqa: BLE001 - recorded with whatever was measured
        facts["verdict"], facts["error"] = "FAIL", str(e)
    finally:
        if not args.keep:
            try:
                wait("the cluster whole before uninstalling", lambda: whole([], args.node), 1200, 5)
            except TimeoutError:
                facts.setdefault("notes", []).append("the cluster was not whole within 20 min; uninstalled anyway")
            for ns in cells:
                subprocess.run(["helm", "uninstall", ns, "-n", ns, "--wait"], capture_output=True, text=True)
                kubectl("delete", "namespace", ns, "--ignore-not-found", "--wait=false", check=False)
    stamp = started.strftime("%Y%m%dT%H%M%SZ")
    out = probe.EVIDENCE / f"many-volumes-{args.cells}cells-{stamp}.json"
    out.write_text(json.dumps(facts, indent=2) + "\n")
    print(json.dumps({k: v for k, v in facts.items() if k != "runs"}, indent=1))
    print("evidence:", out.name)
    return 0 if facts["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
