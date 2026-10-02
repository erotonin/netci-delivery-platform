#!/usr/bin/env python3
"""Can a build's log be read while its cell is taken over? (netci-cell logShipping)

Runs the supervised power-off scenario of probe.py on a 240 s build and, beside it, asks every
second whether Jenkins answers and how many of the build's lines Loki holds. While the controller
is down, its UI and its volume are unavailable; Loki is the only place the log can be read.
Afterwards Loki must hold every line of the build exactly once.

    NETCI_CELL=cell-b NETCI_CELL_PORT=30081 python3 lab/spike/logs_during_takeover.py [--crash-at-tick 40]

Evidence: lab/evidence/logs-during-takeover-<time>.json, beside the probe's own.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import probe  # noqa: E402

LOKI = "/api/v1/namespaces/monitoring/services/loki:3100/proxy/loki/api/v1/query_range"
JOB = "resume-probe"


def loki_ticks(cell: str, build: int, since_ns: int) -> list[int]:
    query = f'{{cell="{cell}", job="{JOB}"}} | build="{build}" |= "tick "'
    q = urllib.parse.urlencode({"query": query, "start": str(since_ns), "end": str(time.time_ns()), "limit": "5000",
                                "direction": "forward"})
    out = probe.kubectl("get", "--raw", f"{LOKI}?{q}", check=False, timeout=15)
    try:
        result = json.loads(out)["data"]["result"]
    except (ValueError, KeyError, TypeError):
        return []
    return [int(m.group(1)) for s in result for _, line in s["values"] if (m := re.match(r"tick (\d+) ", line))]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--crash-at-tick", type=int, default=40)
    args = ap.parse_args()
    cell = os.environ.get("NETCI_CELL", "cell-a")
    j = probe.Jenkins()
    build = j.get_json(f"/job/{JOB}/api/json?tree=nextBuildNumber")["nextBuildNumber"]
    since_ns = time.time_ns() - 60 * 10**9
    started = time.monotonic()
    timeline: list[dict] = []
    done = threading.Event()

    def watch() -> None:
        last = None
        while not done.is_set():
            up = j.up()
            ticks = loki_ticks(cell, build, since_ns)
            state = (up, len(ticks))
            if state != last:
                timeline.append({"t": round(time.monotonic() - started, 1), "jenkinsAnswers": up,
                                 "ticksInLoki": len(ticks), "lastTickInLoki": max(ticks, default=0)})
                last = state
            time.sleep(1)

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    run = subprocess.run([sys.executable, str(Path(__file__).resolve().parent / "probe.py"), "resume", "--failure",
                          "node-poweroff-supervised", "--seconds", "240", "--crash-at-tick", str(args.crash_at_tick),
                          "--job", JOB], capture_output=True, text=True)
    time.sleep(5)  # the shipper flushes every second
    done.set()
    watcher.join()

    ticks = loki_ticks(cell, build, since_ns)
    down = [e for e in timeline if not e["jenkinsAnswers"]]
    readable_while_down = max((e["lastTickInLoki"] for e in down), default=0)
    facts = {
        "scenario": "logs-during-takeover", "cell": cell, "job": JOB, "build": build,
        "crashAtTick": args.crash_at_tick, "probeExit": run.returncode, "probeOutputTail": run.stdout.splitlines()[-5:],
        "timeline": timeline,
        "lastTickReadableWhileJenkinsDown": readable_while_down,
        "ticksInLoki": len(ticks), "distinctTicksInLoki": len(set(ticks)),
        "missing": sorted(set(range(1, 241)) - set(ticks)), "duplicated": sorted({t for t in ticks if ticks.count(t) > 1}),
    }
    ok = (run.returncode == 0 and down and readable_while_down >= args.crash_at_tick - 2
          and not facts["missing"] and not facts["duplicated"])
    facts["verdict"] = "PASS" if ok else "FAIL"
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = probe.EVIDENCE / f"logs-during-takeover-{stamp}.json"
    out.write_text(json.dumps(facts, indent=2) + "\n")
    print(json.dumps({k: v for k, v in facts.items() if k not in ("timeline", "probeOutputTail")}, indent=1))
    print("evidence:", out.name)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
