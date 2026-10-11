#!/usr/bin/env python3
"""Where a build's seconds go, on a fabric sandbox and on a Kubernetes-plugin pod (ADR-064 found
the fabric slower on an idle lab, 5.9 s against 4.4 s, without saying where).

    python3 lab/spike/fabric_timeline.py [--runs 5]

Runs fabric-probe and k8s-probe (the same one-line `sh` step) one at a time and, for each build,
reads from Jenkins' own records -- the console with timestamps (the timestamper plugin, enabled for
every Pipeline in the lab's JCasC) -- and, for the fabric, the fabric's "claimed" log line:
  trigger -> build started -> (claimed) -> "Running on" the agent -> the step's output -> finished.
Evidence: lab/evidence/fabric-timeline-<timestamp>.json.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import statistics
import sys
import time
from pathlib import Path

os.environ.setdefault("NETCI_CELL", "cell-b")
os.environ.setdefault("NETCI_CELL_PORT", "30081")
sys.path.insert(0, str(Path(__file__).parent))
import probe  # noqa: E402

UTC = dt.timezone.utc


def stamped(j: probe.Jenkins, job: str, number: int) -> list[tuple[float, str]]:
    """The console as (epoch seconds, line), from the timestamper plugin."""
    raw = j._request(f"/job/{job}/{number}/timestamps/?time=yyyy-MM-dd'T'HH:mm:ss.SSS&timeZone=UTC&appendLog").decode()
    out = []
    for line in raw.splitlines():
        m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3})\s+(.*)", line)
        if m:
            out.append((dt.datetime.fromisoformat(m.group(1)).replace(tzinfo=UTC).timestamp(), m.group(2)))
    return out


def claimed_at(agent: str) -> float | None:
    lead = probe.kubectl("-n", "netci-system", "get", "lease", "netci-fabric", "-o", "jsonpath={.spec.holderIdentity}", check=False)
    for line in probe.kubectl("-n", "netci-system", "logs", lead, "--since=10m", check=False).splitlines():
        if '"msg":"claimed"' in line and f'"agent":"{agent}"' in line:
            return dt.datetime.fromisoformat(json.loads(line)["time"].replace("Z", "+00:00")).timestamp()
    return None


def run_once(j: probe.Jenkins, job: str) -> dict:
    t0 = time.time()
    number = j.trigger(job)
    probe.wait(f"{job} #{number} done", lambda: (b := j.build(job, number)) and not b["building"] and b.get("result"), 600, 0.2)
    b = j.get_json(f"/job/{job}/{number}/api/json?tree=result,timestamp,duration")
    lines = stamped(j, job, number)
    first = lambda pat: next((t for t, l in lines if re.search(pat, l)), None)
    running = next(((t, l) for t, l in lines if l.startswith("Running on ")), (None, ""))
    agent = (running[1].split()[2] if running[0] else "").strip("‘’'")
    marks = {"started": b["timestamp"] / 1000, "runningOn": running[0], "stepOutput": first(r"^on |^0 "),
             "finished": (b["timestamp"] + b["duration"]) / 1000}
    if job.startswith("fabric"):
        marks["claimed"] = claimed_at(agent)
    return {"build": number, "result": b["result"], "agent": agent, "triggerToDone": round(marks["finished"] - t0, 2),
            "fromTrigger": {k: (round(v - t0, 2) if v else None) for k, v in marks.items()}}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--burst", type=int, default=0, help="then this many builds of each job at once, --runs times")
    args = ap.parse_args()
    started = dt.datetime.now(UTC)
    j = probe.Jenkins()
    results: dict[str, list[dict]] = {"fabric-probe": [], "k8s-probe": []}
    for _ in range(args.runs):
        for job in results:
            results[job].append(run_once(j, job))
            time.sleep(5)  # the warm pool refills, as between builds
    if args.burst:
        from concurrent.futures import ThreadPoolExecutor
        for job in ("fabric-probe", "k8s-probe"):
            key = f"{job} (burst of {args.burst})"
            results[key] = []
            for _ in range(args.runs):
                with ThreadPoolExecutor(args.burst) as ex:
                    results[key] += list(ex.map(lambda _: run_once(probe.Jenkins(), job), range(args.burst)))
                time.sleep(30)  # the warm pool refills before the next burst
    facts: dict = {"scenario": "fabric-timeline", "started": started.isoformat(timespec="seconds"), "runs": args.runs,
                   "burst": args.burst, "builds": results}
    for job, rs in results.items():
        facts[job] = {k: statistics.median([r["fromTrigger"][k] for r in rs if r["fromTrigger"].get(k) is not None])
                      for k in rs[0]["fromTrigger"] if any(r["fromTrigger"].get(k) is not None for r in rs)}
        facts[job]["triggerToDone"] = statistics.median(r["triggerToDone"] for r in rs)
    facts["verdict"] = "PASS" if all(r["result"] == "SUCCESS" for rs in results.values() for r in rs) else "FAIL"
    out = probe.EVIDENCE / f"fabric-timeline-{started.strftime('%Y%m%dT%H%M%SZ')}.json"
    out.write_text(json.dumps(facts, indent=2) + "\n")
    print(json.dumps({k: v for k, v in facts.items() if k != "builds"}, indent=1))
    print("evidence:", out.name)
    return 0 if facts["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
