#!/usr/bin/env python3
"""Active-active, a soak (ADR-080): one fault after another under steady use, and after all of them:
was any acknowledged edit lost, any build lost or run twice, any number given twice?

    python3 lab/aa/phase6_soak.py [--rounds N] [--only FAULT] [--watch]

--watch keeps every replica's and the store's log, and a thread dump of each live replica every
20 s while a round waits for its build, in .netci-gate/aa/watch-<timestamp>/ (not evidence: logs).

Steady use for the whole run: a reader asks the VIP for /api/json each second; two writers, one
pinned to each replica, create a job each second. Each round starts `long` (90 one-second ticks on
an inbound agent) and, once it is 10 ticks in, injects the round's fault, cycling through:
  jenkins-pod        the build's replica's pod killed (no grace)
  store-pod          the store's primary pod killed (no grace)
  homesync-pod       one netci-homesync pod killed
  haproxy-pod        the HAProxy pod of the machine holding the VIP killed
  jenkins-machine    the build's replica's machine powered off, on again after 150 s
  store-machine      the store's primary machine powered off, on again after 150 s
A machine is never one with the agents on it: a build whose agent dies fails in any Jenkins, and
that is not what is under test. When the target is on that machine, the round kills the pod
instead and says so. Each round waits for the build to finish and for everything to be whole again.
Checked at the end: every create answered 200 exists on both replicas; each round's build finished
SUCCESS with every tick once; the numbers of `long` are distinct and each round's build is listed
on both replicas.
Evidence: lab/evidence/aa-phase6-soak-<timestamp>.json.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import phase3_probe as p3  # noqa: E402
import phase4_probe as p4  # noqa: E402
import phase5_probe as p5  # noqa: E402
import phase6_store_probe as p6  # noqa: E402

NS = p3.NS
VIRSH = ["virsh", "-c", "qemu:///system"]
FAULTS = ["jenkins-pod", "store-pod", "homesync-pod", "haproxy-pod", "jenkins-machine", "store-machine"]
OFF_FOR = 150


def pods() -> list[dict]:
    return json.loads(p3.k("-n", NS, "get", "pods", "-o", "json"))["items"]


def node_of(name: str) -> str:
    return next(p["spec"]["nodeName"] for p in pods() if p["metadata"]["name"] == name)


def kill_pod(name: str) -> None:
    p3.k("-n", NS, "delete", "pod", name, "--grace-period=0", "--force")


def vip_holder() -> str:
    lease = json.loads(p3.k("-n", NS, "get", "lease", "-o", "json"))["items"]
    holders = [x["spec"].get("holderIdentity", "") for x in lease if "vip" in x["metadata"]["name"]]
    return holders[0] if holders else ""


class Load:
    """The steady use: writers and a reader, recording absolute times."""

    def __init__(self, run: str) -> None:
        self.run, self.stop = run, threading.Event()
        self.writes: dict[str, list] = {"r0": [], "r1": []}
        self.reads: list = []
        self.threads = [threading.Thread(target=self.writer, args=(r,), daemon=True) for r in self.writes] + \
            [threading.Thread(target=self.reader, daemon=True)]

    def writer(self, r: str) -> None:
        c, i = p3.R(r), 0
        while not self.stop.is_set():
            i += 1
            name, at = f"soak-{self.run}-{r}-{i}", time.time()
            code, _, served = c.req(f"/createItem?name={name}", p3.JOB_XML.format(d="soak").encode(), "application/xml", timeout=15)
            self.writes[r].append({"name": name, "at": round(at, 1), "code": code, "served": served, "took": round(time.time() - at, 2)})
            if code == 403:
                c.crumb = None
            time.sleep(max(0.0, 1 - (time.time() - at)))

    def reader(self) -> None:
        auth = p3.R("r0").auth
        while not self.stop.is_set():
            at = time.time()
            try:
                with urllib.request.urlopen(urllib.request.Request(p3.VIP + "/api/json?tree=mode", headers={"Authorization": auth}),
                                            timeout=3) as resp:
                    ok = resp.status == 200
            except OSError:
                ok = False
            self.reads.append({"at": round(at, 1), "ok": ok})
            time.sleep(max(0.0, 1 - (time.time() - at)))


WATCH: Path | None = None


def watch_logs(stop: threading.Event) -> list[subprocess.Popen]:
    """Every replica's, homesync's and the store's log, followed to files for the whole run."""
    procs = []
    for p in pods():
        name = p["metadata"]["name"]
        if name.startswith(("jenkins-", "homesync-", "store-")):
            f = open(WATCH / f"{name}.log", "a")  # noqa: SIM115 - closed with the process
            procs.append(subprocess.Popen(["kubectl", "--kubeconfig", str(p3.p0.STATE / "kubeconfig"), "-n", NS, "logs", "-f",
                                           "--all-containers", "--prefix", name], stdout=f, stderr=subprocess.STDOUT))
    return procs


def dumps(n: int, until: threading.Event) -> None:
    """A thread dump of each replica that answers, every 20 s, while round n waits."""
    while not until.wait(20):
        for r in ("r0", "r1"):
            code, body, served = p3.R(r).req("/threadDump", timeout=10)
            if code == 200 and served == r:
                (WATCH / f"threads-{n}-{r}-{int(time.time())}.html").write_text(body)


def round_(kind: str, agents_node: str) -> dict:
    p5.whole()
    p3.until(p6.settled, 900, 5)
    # A replica that has just come back answered one "Build now" 302 (round 3 of a run); the answers
    # are kept, and the build is asked for again, at most 5 times.
    triggers = []
    for _ in range(5):
        code, _ = p4.trigger(p3.R("r0"), "long")
        triggers.append(code)
        if code == 201:
            break
        time.sleep(3)
    assert triggers[-1] == 201, triggers
    p3.until(lambda: any(b["building"] for b in p4.builds(p3.R("r0"), "long")), 120)
    n = max(b["number"] for b in p4.builds(p3.R("r0"), "long") if b["building"])
    if p3.until(lambda: max(p5.ticks(n) or [0]) >= 10, 180, 0.5) is None:
        raise SystemExit(f"long #{n} did not reach tick 10")
    holder, _ = p5.holder(n)
    store_primary = p6.store()["status"]["currentPrimary"]
    target = {"jenkins-pod": p5.REPLICA.get(holder or "r0"), "jenkins-machine": p5.REPLICA.get(holder or "r0"),
              "store-pod": store_primary, "store-machine": store_primary,
              "homesync-pod": next(p["metadata"]["name"] for p in pods() if p["metadata"]["labels"].get("app") == "homesync"),
              "haproxy-pod": next(p["metadata"]["name"] for p in pods() if p["metadata"]["labels"].get("app") == "haproxy"
                                  and p["spec"]["nodeName"] == (vip_holder() or p["spec"]["nodeName"]))}[kind]
    victim_node = node_of(target)
    done = kind
    t0 = time.time()
    if kind.endswith("-machine") and victim_node != agents_node:
        p6.apiserver_not_on(victim_node)
        subprocess.run([*VIRSH, "destroy", victim_node], check=True, capture_output=True)
    else:
        if kind.endswith("-machine"):
            done = kind.replace("-machine", "-pod") + " (its machine has the agents)"
        kill_pod(target)
    print(f"round {kind}: long #{n} on {holder}, {done}: {target} on {victim_node}", flush=True)
    waiting = threading.Event()
    if WATCH:
        threading.Thread(target=dumps, args=(n, waiting), daemon=True).start()
    result = None
    while result is None and time.time() - t0 < 600:
        result = p5.holder(n)[1].get("result")
        time.sleep(2)
    finished = round(time.time() - t0, 1)
    waiting.set()
    if kind.endswith("-machine") and victim_node != agents_node:
        time.sleep(max(0, OFF_FOR - (time.time() - t0)))
        subprocess.run([*VIRSH, "start", victim_node], capture_output=True)
    p5.whole()
    p3.until(p6.settled, 900, 5)
    seen = p5.ticks(n)
    # Listed on both, eventually: the replica that came back may still be catching up with the store.
    since = time.time()
    p3.until(lambda: all(p3.R(r).req(f"/job/long/{n}/api/json?tree=result")[0] == 200 for r in ("r0", "r1")), 60, 1)
    listed = {r: p3.R(r).req(f"/job/long/{n}/api/json?tree=result")[0] == 200 for r in ("r0", "r1")}
    listed_after = round(time.time() - since, 1)
    return {"fault": kind, "done": done, "target": target, "node": victim_node, "build": n, "holder": holder,
            "at": round(t0, 1), "result": result, "finishedAfterSeconds": finished, "ticks": len(seen),
            "missing": sorted(set(range(1, 91)) - set(seen)), "twice": sorted({t for t in seen if seen.count(t) > 1}),
            "listedOn": listed, "listedOnBothAfterSeconds": listed_after, "triggers": triggers}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=len(FAULTS))
    ap.add_argument("--only", choices=FAULTS)
    ap.add_argument("--watch", action="store_true")
    args = ap.parse_args()
    faults = [args.only] if args.only else FAULTS
    started = dt.datetime.now(dt.timezone.utc)
    global WATCH
    if args.watch:
        WATCH = p3.p0.STATE.parent / "aa" / f"watch-{started.strftime('%Y%m%dT%H%M%SZ')}"
        WATCH.mkdir(parents=True)
    p5.whole()
    agents_node = node_of("agent-0")
    assert agents_node == node_of("agent-1"), "the agents are on two machines: pick which one not to power off"
    load = Load(started.strftime("%H%M%S"))
    for t in load.threads:
        t.start()
    followers = watch_logs(load.stop) if WATCH else []
    rounds = []
    out = p3.p0.EVIDENCE / f"aa-phase6-soak-{started.strftime('%Y%m%dT%H%M%SZ')}.json"
    try:
        for i in range(args.rounds):
            rounds.append(round_(faults[i % len(faults)], agents_node))
            print(json.dumps(rounds[-1]), flush=True)
            out.write_text(json.dumps({"partial": True, "rounds": rounds}, indent=2) + "\n")
    finally:
        load.stop.set()
        for t in load.threads:
            t.join(30)
        for f in followers:
            f.terminate()
    p5.whole()
    acked = [w for r in load.writes for w in load.writes[r] if w["code"] == 200]
    numbers = [b["number"] for b in p4.builds(p3.R("r0"), "long")]
    refused = [w for r in load.writes for w in load.writes[r] if w["code"] != 200]
    facts = {"scenario": "aa-phase6-soak", "started": started.isoformat(timespec="seconds"),
             "agentsOn": agents_node, "rounds": rounds,
             "builds": {"allSuccess": all(r["result"] == "SUCCESS" for r in rounds),
                        "everyTickOnce": all(not r["missing"] and not r["twice"] for r in rounds),
                        "listedOnBoth": all(all(r["listedOn"].values()) for r in rounds),
                        "numbersDistinct": len(numbers) == len(set(numbers))},
             "writes": {"acknowledged": len(acked), "notAccepted": len(refused),
                        "notAcceptedCodes": sorted({w["code"] for w in refused}),
                        "missingOn": {r: [w["name"] for w in acked if p3.R(r).job(w["name"]) is None] for r in ("r0", "r1")}},
             "ui": {"polls": len(load.reads), "failed": len([x for x in load.reads if not x["ok"]]),
                    "failedAt": [x["at"] for x in load.reads if not x["ok"]]},
             "load": {"writes": load.writes}}
    print(json.dumps({k: v for k, v in facts.items() if k != "load"}, indent=1), flush=True)
    out.write_text(json.dumps(facts, indent=2) + "\n")
    print("evidence:", out.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
