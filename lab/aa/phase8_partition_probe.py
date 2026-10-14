#!/usr/bin/env python3
"""Active-active, a partition (ADR-084): the machine running a build's owner is cut off from the
network -- still running, its Jenkins still alive -- and must not have its build taken over until
its power is confirmed off.

    python3 lab/aa/phase8_partition_probe.py

Needs the replicas on the strict fence (NETCI_ADOPT_FENCE=kubernetes-power, controller netci29+),
the supervisor on lost-node fencing (netci.io/fence-on-loss=true on the replicas' machines) with a
power controller for them (the lab's Redfish emulator). `long` runs on an inbound agent; at tick 10
the owner's machine loses its link (`virsh domif-setlink ... down`: a cable pulled, the VM running).
Recorded, each in seconds after the link went down: the node NotReady, the supervisor's taint (its
machine confirmed off), the adoption (the adopting replica's own log line), the build's end.
Passing: the adoption comes after the taint, the build is SUCCESS, every tick once.
Evidence: lab/evidence/aa-phase8-partition-<timestamp>.json.
"""
from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import phase3_probe as p3  # noqa: E402
import phase4_probe as p4  # noqa: E402
import phase5_probe as p5  # noqa: E402
import phase6_store_probe as p6  # noqa: E402

NS = p3.NS
VIRSH = ["virsh", "-c", "qemu:///system"]


def ts(s: str) -> float:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def main() -> int:
    started = dt.datetime.now(dt.timezone.utc)
    p5.whole()
    code, _ = p4.trigger(p3.R("r0"), "long")
    assert code == 201, code
    p3.until(lambda: any(b["building"] for b in p4.builds(p3.R("r0"), "long")), 120)
    n = max(b["number"] for b in p4.builds(p3.R("r0"), "long") if b["building"])
    if p3.until(lambda: max(p5.ticks(n) or [0]) >= 10, 180, 0.5) is None:
        raise SystemExit(f"long #{n} did not reach tick 10")
    holder, _ = p5.holder(n)
    pod = p5.REPLICA[holder or "r0"]
    victim = p3.k("-n", NS, "get", "pod", pod, "-o", "jsonpath={.spec.nodeName}")
    iface = next(line.split()[0] for line in subprocess.run([*VIRSH, "domiflist", victim], capture_output=True, text=True,
                                                             check=True).stdout.splitlines()[2:] if line.strip())
    p6.apiserver_not_on(victim)
    t0 = time.time()
    subprocess.run([*VIRSH, "domif-setlink", victim, iface, "down"], check=True, capture_output=True)
    print(f"long #{n} on {holder} ({pod}); {victim}'s link {iface} down", flush=True)

    marks: dict = {}
    while time.time() - t0 < 600:
        node = json.loads(p3.k("-n", NS, "get", "node", victim, "-o", "json", check=False) or "{}")
        ready = next((c["status"] for c in node.get("status", {}).get("conditions", []) if c["type"] == "Ready"), "?")
        if ready != "True" and "notReady" not in marks:
            marks["notReady"] = round(time.time() - t0, 1)
        taint = next((t for t in node.get("spec", {}).get("taints", []) or [] if t["key"] == "node.kubernetes.io/out-of-service"), None)
        if taint and "fenced" not in marks:
            marks["fenced"] = round(ts(taint["timeAdded"]) - t0, 1) if taint.get("timeAdded") else round(time.time() - t0, 1)
        state = subprocess.run([*VIRSH, "domstate", victim], capture_output=True, text=True).stdout.strip()
        if state != "running" and "machineOff" not in marks:
            marks["machineOff"] = round(time.time() - t0, 1)
        result = p5.holder(n)[1].get("result")
        if result:
            marks["finished"], marks["result"] = round(time.time() - t0, 1), result
            break
        time.sleep(2)
    for name in p5.REPLICA.values():
        for line in p3.k("-n", NS, "logs", name, "--since=30m", check=False).splitlines():
            if f"adopted long #{n} " in line:
                at = dt.datetime.strptime(line[:23], "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=dt.timezone.utc).timestamp()
                marks["adopted"] = round(at - t0, 1)
                marks["adoptedBy"] = name
    seen = p5.ticks(n)
    if subprocess.run([*VIRSH, "domstate", victim], capture_output=True, text=True).stdout.strip() == "running":
        subprocess.run([*VIRSH, "domif-setlink", victim, iface, "up"], capture_output=True)
    else:
        subprocess.run([*VIRSH, "start", victim], capture_output=True)
    p5.whole()
    facts = {"scenario": "aa-phase8-partition", "started": started.isoformat(timespec="seconds"), "build": n, "owner": holder,
             "victim": victim, "marks": marks, "ticks": len(seen), "missing": sorted(set(range(1, 91)) - set(seen)),
             "twice": sorted({t for t in seen if seen.count(t) > 1}),
             "adoptedOnlyAfterFenced": "adopted" in marks and "fenced" in marks and marks["adopted"] >= marks["fenced"],
             "resumedTwice": p3.R("r1").req(f"/job/long/{n}/consoleText")[1].count("Resuming build") > 1}
    print(json.dumps(facts, indent=1), flush=True)
    out = p3.p0.EVIDENCE / f"aa-phase8-partition-{started.strftime('%Y%m%dT%H%M%SZ')}.json"
    out.write_text(json.dumps(facts, indent=2) + "\n")
    print("evidence:", out.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
