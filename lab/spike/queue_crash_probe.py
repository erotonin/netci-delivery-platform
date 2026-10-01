#!/usr/bin/env python3
"""Lab probe for ADR-063: does a run accepted by netCI survive its controller crashing while the
run waits in Jenkins' queue -- and run exactly once?

    lab/spike/queue_crash_probe.py [--runs 5]

It submits --runs runs of queue-probe-short (20 s quiet period) through netci-queue, and one
control trigger straight to Jenkins, as a person or a webhook would without netCI. When all of
them sit in Jenkins' queue it SIGKILLs the controller's JVM. Then it checks, from Jenkins'
own build records:
  - every netCI run finished, with exactly one build carrying its run id;
  - the control trigger never ran (JENKINS-30909: the queue died with the JVM).
Evidence: lab/evidence/queue-crash-<timestamp>.json.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / ".netci-gate" / "lab"
EVIDENCE = ROOT / "lab" / "evidence"
os.environ.setdefault("NETCI_CELL", "cell-b")
os.environ.setdefault("NETCI_CELL_PORT", "30081")
os.environ["KUBECONFIG"] = str(STATE / "kubeconfig")
sys.path.insert(0, str(Path(__file__).parent))
import probe  # noqa: E402  (the spike's Jenkins client)

INTAKE = "http://192.168.122.211:30090"
JOB = "queue-probe-short"
NS = os.environ["NETCI_CELL"]


def intake(method: str, path: str, body: dict | None = None) -> dict:
    token = (STATE / "queue" / "client-token").read_text().strip()
    req = urllib.request.Request(INTAKE + path, method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def wait(what: str, predicate, timeout: float, interval: float = 1.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            v = predicate()
        except Exception:  # noqa: BLE001 - the controller is restarting; keep asking
            v = None
        if v:
            return v
        time.sleep(interval)
    raise TimeoutError(what)


JAVA_PID = r"""for d in /proc/[0-9]*; do
  c=$(tr '\000' ' ' < "$d/cmdline" 2>/dev/null) || continue
  case "$c" in java\ *jenkins.war*) echo "${d#/proc/}";; esac
done; true"""


def builds(j: probe.Jenkins) -> list[dict]:
    tree = "builds[number,result,timestamp,actions[runId,causes[shortDescription]]]"
    return j.get_json(f"/job/{JOB}/api/json?tree={tree}")["builds"]


def run_id_of(b: dict) -> str | None:
    for a in b.get("actions") or []:
        if a and a.get("_class") == "io.netci.jenkins.NetciRunAction":
            return a.get("runId")
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=5)
    args = ap.parse_args()
    started = dt.datetime.now(dt.timezone.utc)
    j = probe.Jenkins()
    before = {b["number"] for b in builds(j)}
    tag = started.strftime("%Y%m%dT%H%M%S")

    runs = [intake("POST", "/v1/runs", {"job": JOB, "idempotencyKey": f"crash-{tag}-{i}"}) for i in range(args.runs)]
    ids = [r["id"] for r in runs]
    j.post(f"/job/{JOB}/build")  # the control: what a trigger without netCI looks like
    wait("all runs in the controller's queue",
         lambda: all(intake("GET", f"/v1/runs/{i}")["state"] == "dispatched" for i in ids), 60)
    queued = len([q for q in j.queue() if q["task"]["name"] == JOB])

    pids = probe.kubectl("-n", NS, "exec", "jenkins-0", "-c", "jenkins", "--", "sh", "-c", JAVA_PID).split()
    assert len(pids) == 1, f"expected one Jenkins JVM, found {pids}"
    probe.kubectl("-n", NS, "exec", "jenkins-0", "-c", "jenkins", "--", "kill", "-9", pids[0])
    t_kill = time.monotonic()
    j.after_restart()

    def all_finished():
        states = [intake("GET", f"/v1/runs/{i}") for i in ids]
        return states if all(s["state"] in ("finished", "cancelled", "refused") for s in states) else None
    final = wait("every run finished", all_finished, 600, 2)
    t_done = time.monotonic()
    # Long enough for the control trigger to have run had the queue survived (20 s quiet period).
    time.sleep(45)
    j.after_restart()
    after = [b for b in builds(j) if b["number"] not in before]
    per_run = {i: [b["number"] for b in after if run_id_of(b) == i] for i in ids}
    control_builds = [b["number"] for b in after if run_id_of(b) is None]
    redispatched = sum(1 for s in final for h in s.get("history", [])
                       if (h.get("detail") or {}).get("why", "").startswith("lost from the queue"))

    facts = {
        "scenario": "queue-crash", "cell": NS, "started": started.isoformat(),
        "netci_runs": len(ids), "queued_in_jenkins_before_kill": queued,
        "netci_runs_finished_success": sum(1 for s in final if s["state"] == "finished" and s.get("result") == "SUCCESS"),
        "netci_runs_redispatched_after_crash": redispatched,
        "builds_per_netci_run": per_run,
        "control_trigger_builds": control_builds,
        "seconds_kill_to_all_finished": round(t_done - t_kill, 1),
        "runs": [{"id": s["id"], "state": s["state"], "result": s.get("result"), "build": s.get("buildNumber"),
                  "history": [h["to"] + ((" (" + h["detail"]["why"] + ")") if (h.get("detail") or {}).get("why") else "")
                              for h in s.get("history", [])]} for s in final],
    }
    ok = (facts["netci_runs_finished_success"] == len(ids)
          and all(len(v) == 1 for v in per_run.values())
          and not control_builds)
    facts["verdict"] = "PASS" if ok else "FAIL"
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE / f"queue-crash-{started.strftime('%Y%m%dT%H%M%SZ')}.json"
    path.write_text(json.dumps(facts, indent=2) + "\n")
    print(json.dumps(facts, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
