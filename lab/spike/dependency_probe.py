#!/usr/bin/env python3
"""When something netCI depends on fails, does netCI refuse rather than pretend?

    python3 lab/spike/dependency_probe.py pg-restart    # PostgreSQL restarts (its pod deleted)
    python3 lab/spike/dependency_probe.py pg-outage     # PostgreSQL down for 90 s
    python3 lab/spike/dependency_probe.py bmc-outage    # the power controller unreachable when a machine dies

pg-restart / pg-outage. A netciOnce block starts while the database is down, and runs are submitted
through netci-queue throughout:
  - the intake refuses while it cannot record a run -- a 503, or no replica ready to connect to
    (its readiness follows the database) -- and never acknowledges one it did not keep: every run
    it accepted runs exactly once;
  - netciOnce waits for the database (up to its 60 s): after a restart the block runs, with one
    marker; after 90 s it fails closed -- the block never ran, no marker, the build says why.

bmc-outage. The Redfish emulator is stopped, then cell-b's machine is powered off:
  - nothing is fenced while the power state is unknown (no taint, the Lease not released), and an
    alert says why;
  - once the emulator is back the machine is fenced and the build, still within the agent's
    five minutes, resumes and finishes SUCCESS.
Evidence: lab/evidence/dependency-<scenario>-<timestamp>.json.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

os.environ.setdefault("NETCI_CELL", "cell-b")
os.environ.setdefault("NETCI_CELL_PORT", "30081")
sys.path.insert(0, str(Path(__file__).parent))
import probe  # noqa: E402
import queue_crash_probe as qp  # noqa: E402
import upgrade_probe as up  # noqa: E402  (Submitter, supervisor_events, taints)

ROOT = probe.ROOT
VIRSH = ["virsh", "-c", "qemu:///system"]


def markers(number: int) -> int:
    out = probe.kubectl("-n", "netci-system", "exec", "netci-pg-0", "--", "psql", "-U", "netci", "-d", "netci", "-tAc",
                        f"SELECT count(*) FROM once_markers WHERE client = 'cell-b' AND key = 'deploy' "
                        f"AND scope LIKE '%/once-probe#{number}'")
    return int(out.strip() or 0)


def pg_ready() -> bool:
    return "true" in probe.kubectl("-n", "netci-system", "get", "pod", "netci-pg-0", "-o",
                                   "jsonpath={.status.containerStatuses[0].ready}", check=False)


def runs_exactly_once(j, sub, before: set[int]) -> dict:
    ids = [a["id"] for a in sub.accepted]

    def all_finished():
        states = [qp.intake("GET", f"/v1/runs/{i}") for i in ids]
        return states if all(s["state"] in ("finished", "cancelled", "refused") for s in states) else None
    states = probe.wait("every accepted run finished", all_finished, 900, 3)
    j.after_restart()
    after = [b for b in qp.builds(j) if b["number"] not in before]
    per_run = {i: sum(1 for b in after if qp.run_id_of(b) == i) for i in ids}
    # Not accepted, and said so: a 503 from a replica that cannot record runs, or no replica to
    # connect to -- netci-queue's readiness follows the database, so with it down the Service has no
    # endpoints. Either way the sender knows the run was not taken. Anything else (a timeout, a
    # reset after the request was sent) leaves the sender not knowing, and fails the probe.
    refused = [f for f in sub.failed if "503" in f["error"] or "Connection refused" in f["error"]]
    return {"accepted": len(ids), "refusedClearly": len(refused), "otherFailures": [f for f in sub.failed if f not in refused],
            "finishedSuccess": sum(1 for s in states if s["state"] == "finished" and s.get("result") == "SUCCESS"),
            "notExactlyOnce": {i: n for i, n in per_run.items() if n != 1}}


def pg(scenario: str, facts: dict) -> bool:
    j = probe.Jenkins()
    before = {b["number"] for b in qp.builds(j)}
    sub = up.Submitter(3, facts["tag"])
    sub.start()
    time.sleep(10)  # some runs accepted before the database goes
    t0 = time.monotonic()
    if scenario == "pg-restart":
        probe.kubectl("-n", "netci-system", "delete", "pod", "netci-pg-0", "--wait=false")
    else:
        probe.kubectl("-n", "netci-system", "scale", "statefulset", "netci-pg", "--replicas=0")
    probe.wait("the database down", lambda: not pg_ready(), 60, 0.5)
    facts["databaseDownAfterSeconds"] = round(time.monotonic() - t0, 1)
    # The block starts now, with the database down.
    number = j.trigger("once-probe", {"DURATION": "30"})
    facts["build"] = number
    if scenario == "pg-outage":
        time.sleep(max(0, 90 - (time.monotonic() - t0)))
        probe.kubectl("-n", "netci-system", "scale", "statefulset", "netci-pg", "--replicas=1")
    probe.wait("the database back", pg_ready, 300, 1)
    facts["databaseBackAfterSeconds"] = round(time.monotonic() - t0, 1)
    time.sleep(20)
    sub.stop.set()
    sub.join()
    final = probe.wait("the build to finish", lambda: (b := j.build("once-probe", number)) and not b["building"] and b, 600, 2)
    text = j.console("once-probe", number)
    facts["buildResult"] = final["result"]
    facts["blockRan"] = "tick 1 " in text
    facts["markers"] = markers(number)
    facts["consoleTail"] = [line for line in text.splitlines() if "netciOnce" in line][-4:]
    facts["runs"] = runs_exactly_once(j, sub, before)
    r = facts["runs"]
    queue_ok = not r["notExactlyOnce"] and r["finishedSuccess"] == r["accepted"] and not r["otherFailures"]
    if scenario == "pg-restart":
        return queue_ok and final["result"] == "SUCCESS" and facts["markers"] == 1 and facts["blockRan"]
    return queue_ok and final["result"] == "FAILURE" and facts["markers"] == 0 and not facts["blockRan"]


def bmc(facts: dict) -> bool:
    j = probe.Jenkins()
    number = j.trigger("once-probe", {"DURATION": "240"})
    facts["build"] = number
    probe.wait("the build to tick 20", lambda: len(probe.ticks(j.console("once-probe", number))) >= 20, 300, 0.5)
    node = probe.controller_node()
    holder = probe.kubectl("-n", "cell-b", "get", "lease", "jenkins", "-o", "jsonpath={.spec.holderIdentity}")
    facts["node"] = node
    subprocess.run([str(ROOT / "lab" / "redfish.sh"), "stop"], check=True, capture_output=True)
    since = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    subprocess.run([*VIRSH, "destroy", node], check=True, capture_output=True)
    t0 = time.monotonic()
    unsafe = []

    def watch(stop: threading.Event) -> None:  # anything done to the machine's cell while it is unknown
        while not stop.is_set():
            if node in up.taints():
                unsafe.append(f"{round(time.monotonic() - t0, 1)} s: {node} out of service")
            h = probe.kubectl("-n", "cell-b", "get", "lease", "jenkins", "-o", "jsonpath={.spec.holderIdentity}", check=False)
            if h != holder:
                unsafe.append(f"{round(time.monotonic() - t0, 1)} s: the Lease changed to {h!r}")
            stop.wait(1)
    stop = threading.Event()
    w = threading.Thread(target=watch, args=(stop,), daemon=True)
    w.start()
    time.sleep(60)
    stop.set()
    w.join()
    facts["actedWhileUnknown"] = unsafe
    facts["alerts"] = [e for e in up.supervisor_events(since) if "SupervisorAlert" in e][:3]
    subprocess.run([str(ROOT / "lab" / "redfish.sh"), "start"], check=True, capture_output=True)
    t1 = time.monotonic()
    probe.wait("fenced once the power controller answers", lambda: node in up.taints(), 120, 0.5)
    facts["fencedAfterBmcBackSeconds"] = round(time.monotonic() - t1, 1)
    j.after_restart()
    final = probe.wait("the build to finish", lambda: (b := j.build("once-probe", number)) and not b["building"] and b, 1200, 3)
    text = j.console("once-probe", number)
    facts["buildResult"] = final["result"]
    facts["markers"] = markers(number)
    facts["ticksMissing"] = sorted(set(range(1, 241)) - set(probe.ticks(text)))[:20]
    facts["takeoverSeconds"] = round(time.monotonic() - t0, 1)
    try:
        probe.wait("the machine back on", lambda: subprocess.run([*VIRSH, "domstate", node], capture_output=True,
                                                                  text=True).stdout.strip() == "running", 300, 3)
    except TimeoutError:
        subprocess.run([*VIRSH, "start", node], check=False, capture_output=True)
    return not unsafe and facts["alerts"] != [] and final["result"] == "SUCCESS" and facts["markers"] == 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario", choices=["pg-restart", "pg-outage", "bmc-outage"])
    args = ap.parse_args()
    started = dt.datetime.now(dt.timezone.utc)
    facts: dict = {"scenario": args.scenario, "started": started.isoformat(timespec="seconds"),
                   "tag": f"{args.scenario}-{started.strftime('%Y%m%dT%H%M%S')}"}
    try:
        ok = bmc(facts) if args.scenario == "bmc-outage" else pg(args.scenario, facts)
    except Exception as e:  # noqa: BLE001 - recorded
        ok, facts["error"] = False, str(e)
        if args.scenario == "pg-outage":
            probe.kubectl("-n", "netci-system", "scale", "statefulset", "netci-pg", "--replicas=1", check=False)
        if args.scenario == "bmc-outage":
            subprocess.run([str(ROOT / "lab" / "redfish.sh"), "start"], check=False, capture_output=True)
    facts["verdict"] = "PASS" if ok else "FAIL"
    out = probe.EVIDENCE / f"dependency-{args.scenario}-{started.strftime('%Y%m%dT%H%M%SZ')}.json"
    out.write_text(json.dumps(facts, indent=2) + "\n")
    print(json.dumps(facts, indent=1))
    print("evidence:", out.name)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
