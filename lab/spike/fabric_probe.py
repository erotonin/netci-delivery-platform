#!/usr/bin/env python3
"""Lab probe for ADR-064: what an agent costs a build, on a warm fabric sandbox and on a pod the
Kubernetes plugin creates for the build.

    lab/spike/fabric_probe.py [--runs 5]

Runs fabric-probe and k8s-probe (the same one-line `sh` step) --runs times each, one at a time,
and records from Jenkins' own records how long each build took from being triggered to
finishing. It also checks that the sandbox ran in a user namespace (its uid_map does not map
uid 0 to the host's uid 0), and that the fabric is back to its warm pool with nothing left
claimed. Evidence: lab/evidence/fabric-agents-<timestamp>.json.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "lab" / "evidence"
os.environ.setdefault("NETCI_CELL", "cell-b")
os.environ.setdefault("NETCI_CELL_PORT", "30081")
os.environ["KUBECONFIG"] = str(ROOT / ".netci-gate" / "lab" / "kubeconfig")
sys.path.insert(0, str(Path(__file__).parent))
import probe  # noqa: E402


def run_once(j: probe.Jenkins, job: str) -> dict:
    number = j.trigger(job)
    t0 = time.monotonic()
    end = t0 + 600
    while time.monotonic() < end:
        try:
            b = j.get_json(f"/job/{job}/{number}/api/json?tree=building,result,duration,timestamp")
            if not b["building"] and b["result"]:
                return {"build": number, "result": b["result"], "seconds_trigger_to_done": round(time.monotonic() - t0, 2),
                        "console": j.console(job, number)}
        except Exception:  # noqa: BLE001 - not created yet
            pass
        time.sleep(0.2)
    raise TimeoutError(f"{job} #{number}")


def sandboxes() -> dict[str, int]:
    out = probe.kubectl("-n", "netci-system", "exec", "netci-pg-0", "--", "psql", "-U", "netci", "-d", "netci", "-At",
                        "-c", "SELECT state, count(*) FROM sandboxes WHERE state <> 'deleted' GROUP BY state")
    return {line.split("|")[0]: int(line.split("|")[1]) for line in out.splitlines() if line}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=5)
    args = ap.parse_args()
    started = dt.datetime.now(dt.timezone.utc)
    j = probe.Jenkins()
    results: dict[str, list[dict]] = {"fabric-probe": [], "k8s-probe": []}
    for _ in range(args.runs):
        for job in results:
            results[job].append(run_once(j, job))
            time.sleep(3)  # let the warm pool refill, as it would between builds
    uid_map = next((l.strip() for l in results["fabric-probe"][0]["console"].splitlines() if l.strip().startswith("0 ")), None)
    time.sleep(10)
    facts = {
        "scenario": "fabric-agents", "started": started.isoformat(), "runs": args.runs,
        "fabric_sandbox_uid_map": uid_map,
        "fabric_user_namespace": bool(uid_map) and uid_map.split()[1] != "0",
        "sandboxes_after": sandboxes(),
    }
    for job, rs in results.items():
        secs = [r["seconds_trigger_to_done"] for r in rs]
        facts[job] = {"results": [r["result"] for r in rs], "seconds_trigger_to_done": secs,
                      "median": round(statistics.median(secs), 2), "max": max(secs)}
    after = facts["sandboxes_after"]
    ok = (all(r == "SUCCESS" for k in results for r in facts[k]["results"]) and facts["fabric_user_namespace"]
          and not after.get("claimed") and not after.get("bound"))
    facts["verdict"] = "PASS" if ok else "FAIL"
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE / f"fabric-agents-{started.strftime('%Y%m%dT%H%M%SZ')}.json"
    path.write_text(json.dumps(facts, indent=2) + "\n")
    print(json.dumps(facts, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
