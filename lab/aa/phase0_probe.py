#!/usr/bin/env python3
"""Active-active, phase 0: what breaks when two Jenkins controllers share one JENKINS_HOME and nothing
coordinates them. Not a design -- the evidence a design has to answer.

    python3 lab/aa/phase0_probe.py [--keep]

Deploys lab/aa/phase0.yaml (namespace aa-lab: a Longhorn RWX volume, replicas r1 and r2 of the lab's
controller image, NodePorts 30180/30181), then:
  E1 both start on one home
  E2 a build run on r1: does r2 know it?
  E3 the same job triggered on both at once: which build numbers, which directories
  E4 a job's configuration saved on r1, then on r2: what is on disk
  E5 r1 lost in the middle of a build: what r2 makes of it, now and after it restarts
Evidence: lab/evidence/aa-phase0-<timestamp>.json. --keep leaves the namespace for a look.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import http.cookiejar
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / ".netci-gate" / "lab"
EVIDENCE = ROOT / "lab" / "evidence"
NS = "aa-lab"
HOST = "192.168.122.211"
PORTS = {"r1": 30180, "r2": 30181}
CONTROLLER = "172.17.0.1:8930/netci/jenkins-controller@sha256:1177c784cd43b29608c72ca90eac8979acab9d86a8946cf43bfac0f1ff2c753e"


def k(*args: str, check: bool = True, stdin: str | None = None) -> str:
    # A command that waits (`rollout status --timeout=1200s`) gets its own wait and some: a fixed
    # 120 s killed the upgrade probe in the middle of a rolling restart, before it wrote its evidence.
    waits = [int(a.split("=", 1)[1].rstrip("s")) for a in args if a.startswith("--timeout=") and a.endswith("s")
             and a.split("=", 1)[1].rstrip("s").isdigit()]
    r = subprocess.run(["kubectl", *args], capture_output=True, text=True, input=stdin, timeout=max([120] + [w + 30 for w in waits]),
                       env={"KUBECONFIG": str(STATE / "kubeconfig"), "PATH": "/usr/local/bin:/usr/bin:/bin",
                            # without HOME kubectl caches discovery in the working directory (a .kube/ in the repo)
                            "HOME": str(Path.home())})
    if check and r.returncode:
        raise RuntimeError(f"kubectl {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout.strip()


class J:
    """A Jenkins client for one replica (its own NodePort), with a crumb and a session."""

    def __init__(self, replica: str) -> None:
        self.replica, self.port = replica, PORTS[replica]
        self.auth = base64.b64encode(f"admin:{(STATE / 'cell-admin-password').read_text().strip()}".encode()).decode()
        self.reset()

    def reset(self) -> None:
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.crumb = None

    def req(self, path: str, data: bytes | None = None, ctype: str | None = None, timeout: float = 10) -> tuple[int, bytes, dict]:
        r = urllib.request.Request(f"http://{HOST}:{self.port}{path}", data=data, method="POST" if data is not None else "GET")
        r.add_header("Authorization", f"Basic {self.auth}")
        if ctype:
            r.add_header("Content-Type", ctype)
        if data is not None:
            if self.crumb is None:
                c = json.loads(self.req("/crumbIssuer/api/json")[1])
                self.crumb = (c["crumbRequestField"], c["crumb"])
            r.add_header(*self.crumb)
        try:
            with self.opener.open(r, timeout=timeout) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read(), dict(e.headers)

    def json(self, path: str):
        code, body, _ = self.req(path)
        return json.loads(body) if code == 200 else {"_status": code}

    def up(self) -> bool:
        try:
            return self.req("/api/json?tree=mode", timeout=3)[0] == 200
        except OSError:
            return False

    def trigger(self, job: str, params: dict | None = None) -> int | None:
        path = f"/job/{job}/buildWithParameters" if params else f"/job/{job}/build"
        code, _, headers = self.req(path, urllib.parse.urlencode(params or {}).encode())
        m = re.search(r"/queue/item/(\d+)/", headers.get("Location", ""))
        if not m:
            return None
        for _ in range(120):
            ex = self.json(f"/queue/item/{m.group(1)}/api/json").get("executable") or {}
            if ex.get("number"):
                return ex["number"]
            time.sleep(0.5)
        return None


def wait(what: str, cond, timeout: float, every: float = 1):
    end = time.time() + timeout
    while time.time() < end:
        try:
            v = cond()
        except Exception:  # noqa: BLE001 - keep polling
            v = None
        if v:
            return v
        time.sleep(every)
    raise TimeoutError(what)


def on_home(cmd: str) -> str:
    """A shell command inside r2's pod, on the shared JENKINS_HOME (it sees what both wrote)."""
    pod = k("-n", NS, "get", "pod", "-l", "replica=r2", "-o", "jsonpath={.items[0].metadata.name}")
    return k("-n", NS, "exec", pod, "--", "sh", "-c", cmd, check=False)


def deploy() -> None:
    k("apply", "-f", "-", stdin=(ROOT / "lab/aa/phase0.yaml").read_text().replace("CONTROLLER", CONTROLLER))
    pull = json.loads(k("-n", "cell-b", "get", "secret", "harbor-pull", "-o", "json"))
    for f in ("resourceVersion", "uid", "creationTimestamp", "managedFields", "ownerReferences", "annotations", "labels"):
        pull["metadata"].pop(f, None)
    pull["metadata"]["namespace"] = NS
    k("apply", "-f", "-", stdin=json.dumps(pull))
    k("apply", "-f", "-", stdin=k("-n", NS, "create", "secret", "generic", "admin",
                                  f"--from-file=password={STATE / 'cell-admin-password'}", "--dry-run=client", "-o", "yaml"))
    k("-n", NS, "rollout", "restart", "deployment/jenkins-r1", "deployment/jenkins-r2", check=False)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()
    started = dt.datetime.now(dt.timezone.utc)
    facts: dict = {"scenario": "aa-phase0", "started": started.isoformat(timespec="seconds"), "controller": CONTROLLER}
    deploy()
    r1, r2 = J("r1"), J("r2")
    t0 = time.time()
    wait("both up", lambda: r1.up() and r2.up(), 900, 3)
    facts["E1"] = {"bothUpAfterSeconds": round(time.time() - t0),
                   "nodes": {r: k("-n", NS, "get", "pod", "-l", f"replica={r}", "-o", "jsonpath={.items[0].spec.nodeName}") for r in PORTS},
                   "jobsSeen": {r.replica: [j["name"] for j in r.json("/api/json?tree=jobs[name]").get("jobs", [])] for r in (r1, r2)}}
    print("E1", facts["E1"], flush=True)

    # E2: a build on r1 -- does r2 know it?
    n = r1.trigger("quick")
    wait("quick done on r1", lambda: r1.json(f"/job/quick/{n}/api/json").get("result"), 120)
    facts["E2"] = {"r1Build": n, "r1Last": r1.json("/job/quick/api/json?tree=lastBuild[number]").get("lastBuild"),
                   "r2Last": r2.json("/job/quick/api/json?tree=lastBuild[number]").get("lastBuild"),
                   "r2BuildPage": r2.req(f"/job/quick/{n}/api/json")[0],
                   "onDisk": on_home("ls /var/jenkins_home/jobs/quick/builds | sort -n | tr '\\n' ' '"),
                   "nextBuildNumberFile": on_home("cat /var/jenkins_home/jobs/quick/nextBuildNumber")}
    print("E2", facts["E2"], flush=True)

    # E3: the same job on both at once.
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(2) as ex:
        a, b = ex.submit(r1.trigger, "quick"), ex.submit(r2.trigger, "quick")
        na, nb = a.result(), b.result()
    time.sleep(8)
    facts["E3"] = {"r1Number": na, "r2Number": nb, "sameNumber": na == nb,
                   "onDisk": on_home("ls /var/jenkins_home/jobs/quick/builds | sort -n | tr '\\n' ' '"),
                   "nextBuildNumberFile": on_home("cat /var/jenkins_home/jobs/quick/nextBuildNumber"),
                   "logOfThatNumber": on_home(f"tail -2 /var/jenkins_home/jobs/quick/builds/{na}/log 2>/dev/null | tr '\\n' '|'"),
                   "r1Result": r1.json(f"/job/quick/{na}/api/json?tree=result").get("result"),
                   "r2Result": r2.json(f"/job/quick/{nb}/api/json?tree=result").get("result")}
    print("E3", facts["E3"], flush=True)

    # E4: the job's description saved on r1, then something else saved on r2.
    r1.req("/job/quick/submitDescription", urllib.parse.urlencode({"description": "set on r1"}).encode(), "application/x-www-form-urlencoded")
    on_r2_before = r2.json("/job/quick/api/json?tree=description").get("description")
    r2.req("/job/quick/disable", b"")
    facts["E4"] = {"r2SawR1Description": on_r2_before,
                   "configOnDiskAfterR2Saved": {"description": on_home("grep -o '<description>[^<]*</description>' /var/jenkins_home/jobs/quick/config.xml"),
                                                 "disabled": on_home("grep -o '<disabled>[^<]*</disabled>' /var/jenkins_home/jobs/quick/config.xml")},
                   "r1StillShows": r1.json("/job/quick/api/json?tree=description,buildable")}
    r2.req("/job/quick/enable", b"")
    print("E4", facts["E4"], flush=True)

    # E5: r1 lost in the middle of a build that runs on it.
    m = r1.trigger("tick", {"DURATION": "60"})
    wait("ticking", lambda: "tick 8 " in r1.req(f"/job/tick/{m}/consoleText")[1].decode(errors="replace"), 180)
    k("-n", NS, "scale", "deployment/jenkins-r1", "--replicas=0")
    k("-n", NS, "delete", "pod", "-l", "replica=r1", "--grace-period=0", "--force", check=False)
    time.sleep(5)
    facts["E5"] = {"build": m, "r2SeesItNow": r2.req(f"/job/tick/{m}/api/json?tree=building,result")[0],
                   "onDisk": on_home(f"ls /var/jenkins_home/jobs/tick/builds/{m} 2>&1 | tr '\\n' ' '")}
    # What a restart of r2 makes of r1's interrupted build (the closest thing to "adoption" Jenkins has).
    k("-n", NS, "rollout", "restart", "deployment/jenkins-r2")
    time.sleep(10)
    r2.reset()
    wait("r2 back", r2.up, 600, 3)
    time.sleep(20)
    b = r2.json(f"/job/tick/{m}/api/json?tree=building,result")
    facts["E5"]["afterR2Restart"] = {"build": b, "consoleTail": r2.req(f"/job/tick/{m}/consoleText")[1].decode(errors="replace").splitlines()[-6:]}
    print("E5", facts["E5"], flush=True)

    out = EVIDENCE / f"aa-phase0-{started.strftime('%Y%m%dT%H%M%SZ')}.json"
    out.write_text(json.dumps(facts, indent=2) + "\n")
    print("evidence:", out.name)
    if not args.keep:
        k("delete", "namespace", NS, "--wait=false", check=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
