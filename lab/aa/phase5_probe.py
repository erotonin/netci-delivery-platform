#!/usr/bin/env python3
"""Active-active, phase 5 (ADR-079): does a running build survive its replica's death, by itself?

    python3 lab/aa/phase5_probe.py [--only pod|machine]

Needs phases 2, 3 and 5 applied with a controller of netci18 or later (adoption on, the Kubernetes
fence). `long` runs 90 one-second ticks on an inbound agent. At tick 15 its owner dies:
  pod      its pod killed (no grace), left to Kubernetes -- it comes back on its machine
  machine  its machine's power cut (virsh destroy), powered on again afterwards
Nothing is done by hand after the death. Watched every second through the VIP: which replica has
the build, the ticks in its log. At the end: the result, every tick once or not, and whether the
replica that came back left the build alone.
Evidence: lab/evidence/aa-phase5-<timestamp>.json.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import phase3_probe as p3  # noqa: E402
import phase4_probe as p4  # noqa: E402

NS = p3.NS
VIRSH = ["virsh", "-c", "qemu:///system"]
REPLICA = {"r0": "jenkins-0", "r1": "jenkins-1"}


def kubectl(*args: str, check: bool = True) -> str:
    return p3.k(*args, check=check)


def holder(n: int) -> tuple[str | None, dict]:
    """Which replica answers for build n (a replica without it redirects to the owner), and its state."""
    c = p3.R("r0")
    code, body, served = c.req(f"/job/long/{n}/api/json?tree=building,result")
    return (served if code == 200 else None), (json.loads(body) if code == 200 else {})


def console(n: int) -> str:
    """The build's log from whichever replica answers with it (one may be dead, one may lack it)."""
    best = ""
    for r in ("r0", "r1"):
        code, text, _ = p3.R(r).req(f"/job/long/{n}/consoleText")
        if code == 200 and len(text) > len(best):
            best = text
    return best


def ticks(n: int) -> list[int]:
    return [int(m.group(1)) for m in re.finditer(r"^tick (\d+) on", console(n), re.M)]


def whole() -> None:
    p3.until(lambda: kubectl("get", "nodes", "-o", "jsonpath={range .items[*]}{.status.conditions[?(@.type=='Ready')].status} {end}",
                             check=False).split() == ["True"] * 3, 900, 5)
    kubectl("-n", NS, "rollout", "status", "statefulset/jenkins", "--timeout=900s", check=False)
    p3.until(lambda: all(p3.R(r).req("/api/json")[2] == r for r in ("r0", "r1")), 600, 3)
    p3.until(lambda: all(json.loads(p3.R(r).req(f"/computer/agent-{r[1]}/api/json?tree=offline")[1]).get("offline") is False
                         for r in ("r0", "r1")), 300, 3)


def scenario(kind: str) -> dict:
    whole()
    before = {r: kubectl("-n", NS, "get", "pod", REPLICA[r], "-o", "jsonpath={.spec.nodeName}") for r in REPLICA}
    code, _ = p4.trigger(p3.R("r0"), "long")
    assert code == 201, code
    def running() -> int | None:
        b = [x for x in p4.builds(p3.R("r0"), "long") if x["building"]]
        return b[0]["number"] if b else None
    p3.until(lambda: running() is not None, 60)
    n = running()
    p3.until(lambda: max(ticks(n) or [0]) >= 15, 120, 0.5)
    owner, _ = holder(n)
    victim = before[owner]
    at_tick = max(ticks(n))
    t0 = time.time()
    if kind == "pod":
        kubectl("-n", NS, "delete", "pod", REPLICA[owner], "--grace-period=0", "--force")
    else:
        subprocess.run([*VIRSH, "destroy", victim], check=True, capture_output=True)
    print(f"[{kind}] {owner} ({victim}) killed at tick {at_tick}", flush=True)
    timeline = []
    while time.time() - t0 < 300:
        h, state = holder(n)
        timeline.append({"t": round(time.time() - t0, 1), "holder": h, "building": state.get("building"), "result": state.get("result")})
        if state.get("result"):
            break
        time.sleep(1)
    other = [t for t in timeline if t["holder"] and t["holder"] != owner and t["building"]]
    seen = ticks(n)
    facts = {"build": n, "owner": owner, "victim": victim, "killedAtTick": at_tick,
             "adoptedBy": other[0]["holder"] if other else None,
             "adoptedAfterSeconds": other[0]["t"] if other else None,
             "finishedAfterSeconds": timeline[-1]["t"] if timeline and timeline[-1]["result"] else None,
             "result": timeline[-1]["result"] if timeline else None,
             "ticks": len(seen), "missing": sorted(set(range(1, 91)) - set(seen)),
             "twice": sorted({t for t in seen if seen.count(t) > 1}),
             "hosts": sorted(set(re.findall(r"^tick \d+ on (\S+)", console(n), re.M))),
             "timeline": timeline}
    if kind == "machine":
        subprocess.run([*VIRSH, "start", victim], capture_output=True)
    whole()
    # The replica that came back: it lists the build finished, and did not run it again.
    back = p3.R(owner)
    code, body, served = back.req(f"/job/long/{n}/api/json?tree=building,result")
    facts["returned"] = {"served": served, "state": json.loads(body) if code == 200 else code,
                         "logMentionsResumingTwice": back.req(f"/job/long/{n}/consoleText")[1].count("Resuming build") > 1}
    print(f"[{kind}]", json.dumps({k: v for k, v in facts.items() if k != "timeline"}), flush=True)
    return facts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=["pod", "machine"])
    args = ap.parse_args()
    started = dt.datetime.now(dt.timezone.utc)
    facts: dict = {"scenario": "aa-phase5", "started": started.isoformat(timespec="seconds")}
    for kind in ([args.only] if args.only else ["pod", "machine"]):
        facts[kind] = scenario(kind)
    out = p3.p0.EVIDENCE / f"aa-phase5-{started.strftime('%Y%m%dT%H%M%SZ')}.json"
    out.write_text(json.dumps(facts, indent=2) + "\n")
    print("evidence:", out.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
