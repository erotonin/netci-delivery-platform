#!/usr/bin/env python3
"""Active-active, a rolling upgrade (ADR-080): the controller image changed on both replicas, one at
a time, while people use it. Does the UI or API ever fail, and are edits refused?

    CONTROLLER=<image@digest> python3 lab/aa/phase6_upgrade_probe.py
    python3 lab/aa/phase6_upgrade_probe.py --restart     the same roll, the image unchanged
    python3 lab/aa/phase6_upgrade_probe.py --during "lab/aa/chart.sh upgrade"
                                                          any change (here the Helm chart applied),
                                                          then every workload's rollout awaited

A reader asks the VIP for /api/json twice a second, unpinned (the balancer picks a replica, as for a
person arriving); two writers, one pinned to each replica by the balancer's cookie, create a job each
second. Then the StatefulSet's image is set to CONTROLLER, which restarts jenkins-1, then jenkins-0
(minReadySeconds between them, phase 5), and the run goes on 60 s after the rollout. Every create
answered 200 must exist on both replicas afterwards.
Evidence: lab/evidence/aa-phase6-upgrade-<timestamp>.json.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import phase3_probe as p3  # noqa: E402
import phase5_probe as p5  # noqa: E402

NS = p3.NS


def main() -> int:
    restart = "--restart" in sys.argv[1:]
    during = sys.argv[sys.argv.index("--during") + 1] if "--during" in sys.argv[1:] else ""
    image = os.environ.get("CONTROLLER", "")
    if not restart and not during and "@sha256:" not in image:
        raise SystemExit("CONTROLLER=<controller image by digest>, or --restart")
    started = dt.datetime.now(dt.timezone.utc)
    run = started.strftime("%H%M%S")
    p5.whole()
    before = p3.k("-n", NS, "get", "statefulset", "jenkins", "-o", "jsonpath={.spec.template.spec.containers[0].image}")
    stop = threading.Event()
    writes: dict[str, list] = {"r0": [], "r1": []}
    reads: list = []

    def writer(r: str) -> None:
        c, i = p3.R(r), 0
        while not stop.is_set():
            i += 1
            name, at = f"up-{run}-{r}-{i}", time.time()
            code, _, served = c.req(f"/createItem?name={name}", p3.JOB_XML.format(d="rolling upgrade").encode(),
                                    "application/xml", timeout=15)
            writes[r].append({"name": name, "at": at, "code": code, "served": served, "took": round(time.time() - at, 2)})
            if code == 403:
                c.crumb = None  # a crumb from the replica that restarted
            time.sleep(max(0.0, 1 - (time.time() - at)))

    def reader() -> None:
        auth = p3.R("r0").auth
        while not stop.is_set():
            at = time.time()
            served, why = "", ""
            try:
                req = urllib.request.Request(p3.VIP + "/api/json?tree=mode", headers={"Authorization": auth})
                with urllib.request.urlopen(req, timeout=3) as resp:
                    ok, served = resp.status == 200, resp.headers.get("X-Netci-Replica", "")
            except urllib.error.HTTPError as e:
                ok, served, why = False, e.headers.get("X-Netci-Replica", ""), str(e.code)
            except OSError as e:
                ok, why = False, type(e).__name__
            reads.append({"at": at, "ok": ok, "served": served, "why": why})
            time.sleep(max(0.0, 0.5 - (time.time() - at)))

    threads = [threading.Thread(target=writer, args=(r,), daemon=True) for r in writes] + [threading.Thread(target=reader, daemon=True)]
    for t in threads:
        t.start()
    time.sleep(15)
    t0 = time.time()
    if during:
        subprocess.run(during, shell=True, check=True)
        print(f"ran {during}", flush=True)
        for kind in ("daemonset/haproxy", "deployment/homesync", "deployment/queue"):
            p3.k("-n", NS, "rollout", "status", kind, "--timeout=900s")
        p3.k("-n", "kube-system", "rollout", "status", "daemonset/kube-vip", "--timeout=900s")
        image = p3.k("-n", NS, "get", "statefulset", "jenkins", "-o", "jsonpath={.spec.template.spec.containers[0].image}")
    elif restart:
        image = before
        p3.k("-n", NS, "rollout", "restart", "statefulset/jenkins")
        print("restarted", flush=True)
    else:
        p3.k("-n", NS, "set", "image", "statefulset/jenkins", f"jenkins={image}", f"same-secrets={image}")
        print("image set", flush=True)
    p3.k("-n", NS, "rollout", "status", "statefulset/jenkins", "--timeout=1200s")
    rolled = time.time() - t0
    print(f"rolled out in {rolled:.0f} s", flush=True)
    time.sleep(60)
    stop.set()
    for t in threads:
        t.join(30)
    for x in [*writes["r0"], *writes["r1"], *reads]:
        x["t"] = round(x.pop("at") - t0, 1)
    p5.whole()
    acked = [w for r in writes for w in writes[r] if w["code"] == 200]
    facts: dict = {"scenario": "aa-phase6-upgrade", "started": started.isoformat(timespec="seconds"), "restartOnly": restart, "during": during,
                   "imageBefore": before, "imageAfter": image, "rolledOutAfterSeconds": round(rolled, 1),
                   "reads": {"count": len(reads), "failed": [(x["t"], x["why"], x["served"]) for x in reads if not x["ok"]],
                             "servedBy": {r: sum(x["served"] == r for x in reads) for r in ("r0", "r1")}},
                   "acknowledged": len(acked),
                   "missingOn": {r: [w["name"] for w in acked if p3.R(r).job(w["name"]) is None] for r in ("r0", "r1")},
                   "writes": writes}
    for r, ws in writes.items():
        bad = [w for w in ws if w["code"] != 200]
        facts[f"{r}NotAccepted"] = {"count": len(bad), "of": len(ws), "codes": sorted({w["code"] for w in bad}),
                                    "at": [w["t"] for w in bad], "slowest": max((w["took"] for w in ws), default=None)}
    print(json.dumps({k: v for k, v in facts.items() if k != "writes"}, indent=1), flush=True)
    out = p3.p0.EVIDENCE / f"aa-phase6-upgrade-{started.strftime('%Y%m%dT%H%M%SZ')}.json"
    out.write_text(json.dumps(facts, indent=2) + "\n")
    print("evidence:", out.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
