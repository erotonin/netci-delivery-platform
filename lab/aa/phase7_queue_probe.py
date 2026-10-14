#!/usr/bin/env python3
"""Active-active behind netci-queue (ADR-081): does a run accepted by the queue run exactly once,
through the VIP, when the replica running it loses its machine?

    python3 lab/aa/phase7_queue_probe.py

Needs lab/aa/queue.sh and a controller of netci26 or later. The client is the IDP's: a bearer
token, POST /v1/runs with an idempotency key (the IDP's pipeline run id).
  1 steady   10 runs of `quick` (an `sh` step of 5 s on an agent) submitted at once: each finishes
             SUCCESS with exactly one build, whichever replica each call reached
  2 machine  a run of `long` (90 ticks) on its owner; at tick 10 the owner's machine is powered
             off and, while it is off, 6 more runs of `quick` are submitted. Each run must finish
             SUCCESS with one build; `long`'s every tick once
  3 retried  the same idempotency key submitted again: the same run, no second build
Exactly once is counted on the replicas themselves: every build of the job whose netCI cause names
the run, on each replica (their histories are the same; a run built twice shows twice).
Evidence: lab/evidence/aa-phase7-queue-<timestamp>.json.
"""
from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import phase3_probe as p3  # noqa: E402
import phase5_probe as p5  # noqa: E402
import phase6_store_probe as p6  # noqa: E402

NS = p3.NS
VIRSH = ["virsh", "-c", "qemu:///system"]
NODES = {"netci-lab-1": "192.168.122.211", "netci-lab-2": "192.168.122.212", "netci-lab-3": "192.168.122.213"}
TOKEN = (p3.p0.STATE.parent / "aa" / "queue" / "client-token").read_text().strip()
QUICK = ("<?xml version='1.1' encoding='UTF-8'?><flow-definition plugin=\"workflow-job\"><description>queue probe</description>"
         "<keepDependencies>false</keepDependencies><properties><org.jenkinsci.plugins.workflow.job.properties.DurabilityHintJobProperty>"
         "<hint>MAX_SURVIVABILITY</hint></org.jenkinsci.plugins.workflow.job.properties.DurabilityHintJobProperty></properties>"
         "<definition class=\"org.jenkinsci.plugins.workflow.cps.CpsFlowDefinition\" plugin=\"workflow-cps\"><script>"
         "node('linux') { sh 'echo quick on $(hostname); sleep 5' }</script><sandbox>true</sandbox></definition>"
         "<triggers/><disabled>false</disabled></flow-definition>")
down: set[str] = set()


def queue(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    """netci-queue through the NodePort of a machine that is up."""
    last = (0, {})
    for node, ip in NODES.items():
        if node in down:
            continue
        req = urllib.request.Request(f"http://{ip}:30091{path}", method=method,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")
        except OSError as e:
            last = (0, {"error": str(e)})
    return last


refusals: list[dict] = []


def submit(job: str, key: str) -> dict:
    """As the IDP does: retried with the same idempotency key until the queue accepts it. The queue
    refuses while its own database fails over (not ready: no endpoint, the connection refused) --
    refused, not lost; a retry with the key cannot make a second run."""
    first = time.time()
    while True:
        code, run = queue("POST", "/v1/runs", {"job": job, "idempotencyKey": key})
        if code in (200, 201, 202):
            if time.time() - first > 1:
                refusals.append({"key": key, "acceptedAfterSeconds": round(time.time() - first, 1)})
            return run
        if time.time() - first > 300:
            raise SystemExit(f"the queue did not accept {key} in 300 s: {code} {run}")
        time.sleep(2)


def state(run_id: str) -> dict:
    return queue("GET", f"/v1/runs/{run_id}")[1]


def builds_of(job: str, run_id: str) -> dict[str, list[int]]:
    """Each replica's builds of job whose netCI cause is run_id."""
    out = {}
    for r in ("r0", "r1"):
        code, body, served = p3.R(r).req(f"/job/{job}/api/json?tree=builds[number,actions[causes[runId]]]{{0,200}}")
        if code != 200 or served != r:
            out[r] = None
            continue
        out[r] = sorted(b["number"] for b in json.loads(body)["builds"]
                        if any(c.get("runId") == run_id for a in b.get("actions", []) if a for c in a.get("causes", []) or []))
    return out


def finish(runs: dict[str, str], timeout: float) -> dict[str, dict]:
    """Wait for every run to be finished (or the time to pass); their last views."""
    end, views = time.time() + timeout, {}
    while time.time() < end:
        views = {k: state(v) for k, v in runs.items()}
        if all(v.get("state") in ("finished", "refused", "cancelled") for v in views.values()):
            break
        time.sleep(3)
    return views


def verdict(job: str, runs: dict[str, str], views: dict[str, dict]) -> dict:
    out = {}
    for key, rid in runs.items():
        v = views.get(key, {})
        # Listed on both, eventually: a replica just back may still be catching up with the store.
        p3.until(lambda: all(x is not None and len(x) >= 1 for x in builds_of(job, rid).values()), 60, 2)
        b = builds_of(job, rid)
        out[key] = {"run": rid, "state": v.get("state"), "result": v.get("result"), "build": v.get("buildNumber"),
                    "attempts": v.get("attempts"), "buildsOnReplicas": b,
                    "once": all(x is not None and len(x) == 1 for x in b.values())}
    return out


def main() -> int:
    started = dt.datetime.now(dt.timezone.utc)
    tag = started.strftime("%H%M%S")
    p5.whole()
    p3.until(p6.settled, 600, 5)
    if p3.R("r0").job("quick") is None:
        assert p3.R("r0").req("/createItem?name=quick", QUICK.encode(), "application/xml")[0] == 200
        p3.until(lambda: p3.R("r1").job("quick") is not None, 30, 0.5)
    facts: dict = {"scenario": "aa-phase7-queue", "started": started.isoformat(timespec="seconds")}

    # 1 steady
    steady = {f"steady-{tag}-{i}": submit("quick", f"steady-{tag}-{i}")["id"] for i in range(10)}
    facts["steady"] = verdict("quick", steady, finish(steady, 300))
    print("steady", json.dumps({k: (v["state"], v["result"], v["once"]) for k, v in facts["steady"].items()}), flush=True)

    # 2 machine
    long_key = f"long-{tag}"
    long_run = submit("long", long_key)["id"]
    p3.until(lambda: state(long_run).get("state") == "started", 120, 1)
    n = state(long_run)["buildNumber"]
    if p3.until(lambda: max(p5.ticks(n) or [0]) >= 10, 180, 0.5) is None:
        raise SystemExit(f"long #{n} did not reach tick 10")
    holder, _ = p5.holder(n)
    victim = p3.k("-n", NS, "get", "pod", p5.REPLICA[holder or "r0"], "-o", "jsonpath={.spec.nodeName}")
    p6.apiserver_not_on(victim)
    t0 = time.time()
    subprocess.run([*VIRSH, "destroy", victim], check=True, capture_output=True)
    down.add(victim)
    print(f"machine: long #{n} (run {long_run}) on {holder}; {victim} powered off", flush=True)
    during = {}
    for i in range(6):
        during[f"during-{tag}-{i}"] = submit("quick", f"during-{tag}-{i}")["id"]
        time.sleep(5)
    views = finish({long_key: long_run, **during}, 600)
    finished_after = round(time.time() - t0, 1)
    subprocess.run([*VIRSH, "start", victim], capture_output=True)
    down.discard(victim)
    p5.whole()
    p3.until(p6.settled, 900, 5)
    seen = p5.ticks(n)
    facts["machine"] = {"victim": victim, "holder": holder, "build": n, "doneWithin": finished_after,
                        "long": verdict("long", {long_key: long_run}, views)[long_key] | {
                            "ticks": len(seen), "missing": sorted(set(range(1, 91)) - set(seen)),
                            "twice": sorted({t for t in seen if seen.count(t) > 1})},
                        "during": verdict("quick", during, views)}
    print("machine", json.dumps(facts["machine"]["long"]), flush=True)
    print("during", json.dumps({k: (v["state"], v["result"], v["once"]) for k, v in facts["machine"]["during"].items()}), flush=True)

    # 3 retried
    again = submit("long", long_key)
    facts["retried"] = {"sameRun": again["id"] == long_run, "builds": builds_of("long", long_run)}
    print("retried", json.dumps(facts["retried"]), flush=True)

    all_runs = [*facts["steady"].values(), facts["machine"]["long"], *facts["machine"]["during"].values()]
    facts["refusedThenAccepted"] = refusals
    facts["summary"] = {"runs": len(all_runs),
                        "allSuccess": all(r["state"] == "finished" and r["result"] == "SUCCESS" for r in all_runs),
                        "allOnce": all(r["once"] for r in all_runs) and facts["retried"]["sameRun"],
                        "longEveryTickOnce": not facts["machine"]["long"]["missing"] and not facts["machine"]["long"]["twice"]}
    print("summary", json.dumps(facts["summary"]), flush=True)
    out = p3.p0.EVIDENCE / f"aa-phase7-queue-{started.strftime('%Y%m%dT%H%M%SZ')}.json"
    out.write_text(json.dumps(facts, indent=2) + "\n")
    print("evidence:", out.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
