#!/usr/bin/env python3
"""Active-active, the HA store (ADR-080): the machine running the store's primary loses power while
both replicas are being edited. Is any acknowledged edit lost, how long are edits refused, and does
the UI stay up?

    python3 lab/aa/phase6_store_probe.py [--kill primary|standby]

Needs lab/aa/store.sh install (three instances, synchronous to one standby) over phases 2-5. Two
writers, one per replica, create a job each second (a create takes the item's lock, compares and
sets its version and publishes it: the store's whole write path); a reader asks the VIP for /api/json
each second. 10 s in, the primary's machine (or, with --kill standby, a standby's) is powered off
(virsh destroy) and left off for 150 s. A standby's machine lost should cost no edit at all.
Checked afterwards, with the machine back:
  - every create answered 200 exists on both replicas (an acknowledged write the failover forgot
    would be missing on the replica that did not make it, or on both);
  - the build numbers reserved before (home_build_numbers) are all on the promoted primary;
  - which instance was promoted and when, how long each writer was refused, whether the UI answered.
Evidence: lab/evidence/aa-phase6-store-<timestamp>.json.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import phase3_probe as p3  # noqa: E402
import phase5_probe as p5  # noqa: E402

NS = p3.NS
VIRSH = ["virsh", "-c", "qemu:///system"]
OFF_FOR = 150


def kubectl(*args: str, check: bool = True) -> str:
    return p3.k(*args, check=check)


def apiserver_not_on(victim: str) -> None:
    """Talk to a machine's API server that is not the victim's. The lab kubeconfig names lab-1's;
    the first run powered lab-1 off and lost every kubectl call after it, the evidence with them."""
    ips = dict(line.split() for line in kubectl("get", "nodes", "-o",
               "jsonpath={range .items[*]}{.metadata.name} {.status.addresses[0].address}{'\\n'}{end}").splitlines())
    survivor = next(ip for name, ip in sorted(ips.items()) if name != victim)
    conf = p3.p0.STATE / f"kubeconfig-via-{survivor}"
    conf.write_text(re.sub(r"server: https://[0-9.]+", f"server: https://{survivor}", (p3.p0.STATE / "kubeconfig").read_text()))
    conf.chmod(0o600)

    def k(*args: str, check: bool = True, stdin: str | None = None) -> str:
        r = subprocess.run(["kubectl", "--kubeconfig", str(conf), *args], capture_output=True, text=True, input=stdin,
                           timeout=120, env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(Path.home())})
        if check and r.returncode:
            raise RuntimeError(f"kubectl {' '.join(args)}: {r.stderr.strip()}")
        return r.stdout.strip()
    p3.p0.k = k
    print(f"kubectl through {survivor}", flush=True)


def store() -> dict:
    return json.loads(kubectl("-n", NS, "get", "cluster", "store", "-o", "json"))


def sql(pod: str, query: str) -> str:
    return p3.p0.k("-n", NS, "exec", "-i", pod, "-c", "postgres", "--", "psql", "-U", "postgres", "-d", "netci_homesync",
                   "-At", "-v", "ON_ERROR_STOP=1", stdin=query).strip()


def numbers(pod: str) -> dict[str, int]:
    rows = sql(pod, "select job || ' ' || max(number) from home_build_numbers group by job;")
    return {line.rsplit(" ", 1)[0]: int(line.rsplit(" ", 1)[1]) for line in rows.splitlines() if line}


def settled() -> bool:
    s = store().get("status", {})
    # ANY 1 (quorum) replication reports its standbys as "quorum", FIRST n as "sync".
    states = sql(s["currentPrimary"], "select string_agg(sync_state, ',') from pg_stat_replication;").split(",")
    return s.get("readyInstances") == 3 and s.get("currentPrimary") == s.get("targetPrimary") and \
        sum(x in ("quorum", "sync") for x in states) == 2


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kill", choices=["primary", "standby"], default="primary")
    kill = ap.parse_args().kill
    started = dt.datetime.now(dt.timezone.utc)
    run = started.strftime("%H%M%S")
    p5.whole()
    if p3.until(settled, 600, 5) is None:
        st = store().get("status", {})
        raise SystemExit(f"the store is not three instances with two synchronous candidates: {st.get('phase')}, {st.get('readyInstances')} ready")
    primary = store()["status"]["currentPrimary"]
    target = primary if kill == "primary" else sorted(i for i in store()["status"]["instanceNames"] if i != primary)[0]
    pods = json.loads(kubectl("-n", NS, "get", "pods", "-o", "json"))["items"]
    victim = next(p["spec"]["nodeName"] for p in pods if p["metadata"]["name"] == target)
    alongside = sorted(p["metadata"]["name"] for p in pods if p["spec"].get("nodeName") == victim and p["metadata"]["name"] != target)
    reserved = numbers(primary)
    apiserver_not_on(victim)
    print(f"{kill} {target} on {victim} (primary {primary}), alongside: {', '.join(alongside)}", flush=True)

    t0: list[float] = []
    stop = threading.Event()
    writes: dict[str, list] = {"r0": [], "r1": []}
    reads: list = []
    primaries: list = []

    def writer(r: str) -> None:
        c, i = p3.R(r), 0
        while not stop.is_set():
            i += 1
            name, at = f"st-{run}-{r}-{i}", time.time()
            code, _, served = c.req(f"/createItem?name={name}", p3.JOB_XML.format(d="store failover").encode(),
                                    "application/xml", timeout=15)
            writes[r].append({"name": name, "at": at, "code": code, "served": served, "took": round(time.time() - at, 2)})
            if code == 403:
                c.crumb = None  # a crumb from a replica that died with the machine
            time.sleep(max(0.0, 1 - (time.time() - at)))

    def reader() -> None:
        while not stop.is_set():
            at = time.time()
            try:
                req = urllib.request.Request(p3.VIP + "/api/json?tree=mode", headers={"Authorization": p3.R("r0").auth})
                with urllib.request.urlopen(req, timeout=3) as resp:
                    ok = resp.status == 200
            except OSError:
                ok = False
            reads.append({"at": at, "ok": ok})
            time.sleep(max(0.0, 1 - (time.time() - at)))

    def watcher() -> None:
        while not stop.is_set():
            try:
                s = store().get("status", {})
                if not primaries or primaries[-1]["primary"] != s.get("currentPrimary") or primaries[-1]["phase"] != s.get("phase"):
                    primaries.append({"at": time.time(), "primary": s.get("currentPrimary"), "phase": s.get("phase")})
            except Exception:  # noqa: BLE001 - the API server on the dead machine may not answer
                pass
            time.sleep(1)

    threads = [threading.Thread(target=writer, args=(r,), daemon=True) for r in writes] + \
        [threading.Thread(target=reader, daemon=True), threading.Thread(target=watcher, daemon=True)]
    for t in threads:
        t.start()
    time.sleep(10)
    t0.append(time.time())
    subprocess.run([*VIRSH, "destroy", victim], check=True, capture_output=True)
    print(f"{victim} powered off", flush=True)
    time.sleep(OFF_FOR)
    stop.set()
    for t in threads:
        t.join(30)
    # Times relative to the power-off (negative before it); recorded absolute, since the threads
    # start before the moment exists.
    for x in [*writes["r0"], *writes["r1"], *reads, *primaries]:
        x["t"] = round(x.pop("at") - t0[0], 1)
    subprocess.run([*VIRSH, "start", victim], capture_output=True)

    out = p3.p0.EVIDENCE / f"aa-phase6-store-{started.strftime('%Y%m%dT%H%M%SZ')}.json"
    out.write_text(json.dumps({"partial": True, "victim": victim, "primaryBefore": primary, "primaryTimeline": primaries,
                               "writes": writes, "reads": reads}, indent=2) + "\n")
    promoted = store()["status"]["currentPrimary"]
    after = numbers(promoted)
    p5.whole()
    p3.until(settled, 900, 5)

    acked = [w for r in writes for w in writes[r] if w["code"] == 200]
    missing = {r: [w["name"] for w in acked if p3.R(r).job(w["name"]) is None] for r in ("r0", "r1")}
    facts: dict = {"scenario": "aa-phase6-store", "started": started.isoformat(timespec="seconds"),
                   "killed": kill, "target": target, "victim": victim, "primaryBefore": primary, "alongside": alongside, "promoted": promoted,
                   "primaryTimeline": primaries,
                   "acknowledged": len(acked), "missingOn": missing,
                   "reservedBefore": reserved, "reservedOnPromoted": after,
                   "numbersLost": {j: n for j, n in reserved.items() if after.get(j, 0) < n},
                   "writes": writes}
    for r, ws in writes.items():
        failed = [w for w in ws if w["code"] != 200 and w["t"] >= 0]
        ok_after = [w for w in ws if w["code"] == 200 and w["t"] >= 0]
        facts[f"{r}Refused"] = {"count": len(failed), "codes": sorted({w["code"] for w in failed}),
                                "firstOkAfterDeath": next((w["t"] + w["took"] for w in ok_after if failed and w["t"] > failed[0]["t"]), None),
                                "lastFailure": (failed[-1]["t"] + failed[-1]["took"]) if failed else None}
    down = [x["t"] for x in reads if not x["ok"] and x["t"] >= 0]
    facts["ui"] = {"polls": len([x for x in reads if x["t"] >= 0]), "failed": len(down), "failedAt": down}
    facts["afterReturn"] = {"status": {k: store()["status"].get(k) for k in ("currentPrimary", "readyInstances", "phase")},
                            "replication": sql(store()["status"]["currentPrimary"],
                                               "select string_agg(application_name || ':' || sync_state, ',' order by application_name) from pg_stat_replication;")}
    print(json.dumps({k: v for k, v in facts.items() if k != "writes"}, indent=1), flush=True)
    out.write_text(json.dumps(facts, indent=2) + "\n")
    print("evidence:", out.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
