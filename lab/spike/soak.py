#!/usr/bin/env python3
"""Hours of random failures, one after another, with the invariants checked after each.

Every probe so far found a defect when its conditions changed a little; a long run of mixed faults
is how the rare ones show. Each round picks a fault by weight and runs the probe that already
measures it -- each checks its own build (SUCCESS, every tick, one netciOnce marker, the neighbour
untouched) -- and then this checks what must hold between faults:
  - every cell's Lease is held by its own running pod;
  - no node is left out of service, every node is Ready, every Longhorn volume healthy (within
    the probes' own wait for a whole cluster);
  - the supervisor has a leader.

    python3 lab/spike/soak.py --hours 4 [--seed N]

Faults (weight): power loss (4), power loss while the supervisor's leader is killed too (2), hung
machine (2), JVM killed (1), controller pod deleted (1), PostgreSQL restarted (1), power controller
unreachable when a machine dies (1).
Evidence: lab/evidence/soak-<timestamp>.json, updated after every round (a soak cut short keeps
what it saw), plus each probe's own evidence.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import random
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import probe  # noqa: E402

ROOT = probe.ROOT
SPIKE = ROOT / "lab" / "spike"
FAULTS = [  # name, weight, command, extra environment
    ("power-loss", 4, ["chaos_poweroff.py", "--runs", "1", "--job", "once-probe"], {}),
    ("power-loss+supervisor-leader", 2, ["chaos_poweroff.py", "--runs", "1", "--job", "once-probe"],
     {"NETCI_ALSO_KILL_SUPERVISOR_LEADER": "1"}),
    ("hung-machine", 2, ["chaos_poweroff.py", "--runs", "1", "--job", "once-probe", "--failure", "node-hang-supervised"], {}),
    ("jvm-kill", 1, ["probe.py", "resume", "--failure", "jvm-kill", "--job", "once-probe"], {}),
    ("pod-delete", 1, ["probe.py", "resume", "--failure", "pod-delete", "--job", "once-probe"], {}),
    ("postgresql-restart", 1, ["dependency_probe.py", "pg-restart"], {}),
    ("bmc-unreachable", 1, ["dependency_probe.py", "bmc-outage"], {}),
]


def invariants() -> list[str]:
    broken = []
    for cell in ("cell-a", "cell-b"):
        holder = probe.kubectl("-n", cell, "get", "lease", "jenkins", "-o", "jsonpath={.spec.holderIdentity}", check=False)
        pod = probe.kubectl("-n", cell, "get", "pod", "jenkins-0", "-o", "jsonpath={.metadata.name}/{.metadata.uid}", check=False)
        if not holder or holder != pod:
            broken.append(f"{cell}: Lease held by {holder!r}, pod is {pod!r}")
    for n in json.loads(probe.kubectl("get", "nodes", "-o", "json"))["items"]:
        if any(t["key"] == "node.kubernetes.io/out-of-service" for t in n["spec"].get("taints") or []):
            broken.append(f"{n['metadata']['name']} left out of service")
    if not probe.kubectl("-n", "netci-system", "get", "lease", "netci-supervisor", "-o", "jsonpath={.spec.holderIdentity}", check=False):
        broken.append("no supervisor leader")
    return broken


def whole() -> bool:
    for n in json.loads(probe.kubectl("get", "nodes", "-o", "json"))["items"]:
        if not any(c["type"] == "Ready" and c["status"] == "True" for c in n["status"]["conditions"]):
            return False
    rob = probe.kubectl("-n", "longhorn-system", "get", "volumes.longhorn.io", "-o", "jsonpath={.items[*].status.robustness}", check=False).split()
    return bool(rob) and all(r == "healthy" for r in rob)


def verdict_of(name: str, out: str, code: int) -> str:
    if name.startswith(("power-loss", "hung")) or name.startswith("bmc") or name.startswith("postgresql"):
        return "PASS" if code == 0 and '"verdict": "PASS"' in out else "FAIL"
    # probe.py resume: exits 0 whatever happened; its build result is the verdict
    return "PASS" if code == 0 and '"result": "SUCCESS"' in out and '"refused": true' not in out else "FAIL"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=4)
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args()
    seed = args.seed if args.seed is not None else int(time.time())
    rnd = random.Random(seed)
    started = dt.datetime.now(dt.timezone.utc)
    out_path = probe.EVIDENCE / f"soak-{started.strftime('%Y%m%dT%H%M%SZ')}.json"
    facts: dict = {"scenario": "soak", "seed": seed, "hours": args.hours, "started": started.isoformat(timespec="seconds"), "rounds": []}
    end = time.monotonic() + args.hours * 3600
    names, weights = [f[0] for f in FAULTS], [f[1] for f in FAULTS]
    while time.monotonic() < end:
        probe.wait("the cluster whole before a fault", whole, 1800, 10)
        name = rnd.choices(names, weights)[0]
        _, _, cmd, extra = next(f for f in FAULTS if f[0] == name)
        t0 = time.monotonic()
        r = subprocess.run([sys.executable, str(SPIKE / cmd[0]), *cmd[1:]], env={**os.environ, **extra},
                           capture_output=True, text=True, timeout=5400)
        # probe.py and the dependency probe print "evidence: <file>"; chaos_poweroff.py puts it in its JSON.
        evidence = [line.split("evidence:")[1].strip() for line in r.stdout.splitlines() if line.startswith("evidence:")]
        evidence += re.findall(r'"evidence": "(chaos-poweroff-[^"]+)"', r.stdout)[-1:]
        rnd_facts = {"round": len(facts["rounds"]) + 1, "fault": name, "seconds": round(time.monotonic() - t0),
                     "exit": r.returncode, "verdict": verdict_of(name, r.stdout, r.returncode), "evidence": evidence[-1:]}
        try:
            probe.wait("the cluster whole after the fault", whole, 1800, 10)
            rnd_facts["invariantsBroken"] = invariants()
        except TimeoutError:
            rnd_facts["invariantsBroken"] = ["the cluster was not whole 30 minutes after the fault"] + invariants()
        if rnd_facts["invariantsBroken"]:
            rnd_facts["verdict"] = "FAIL"
        if rnd_facts["verdict"] == "FAIL":
            rnd_facts["stdoutTail"] = r.stdout.strip().splitlines()[-15:]
            rnd_facts["stderrTail"] = r.stderr.strip().splitlines()[-8:]
        facts["rounds"].append(rnd_facts)
        rounds = facts["rounds"]
        facts["summary"] = {"rounds": len(rounds), "passed": sum(x["verdict"] == "PASS" for x in rounds),
                            "byFault": {n: [x["verdict"] for x in rounds if x["fault"] == n] for n in names if any(x["fault"] == n for x in rounds)}}
        facts["verdict"] = "PASS" if all(x["verdict"] == "PASS" for x in rounds) else "FAIL"
        out_path.write_text(json.dumps(facts, indent=2) + "\n")
        print(json.dumps(rnd_facts), flush=True)
    print(json.dumps(facts.get("summary"), indent=1))
    print("evidence:", out_path.name)
    return 0 if facts.get("verdict") == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
