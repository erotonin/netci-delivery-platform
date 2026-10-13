#!/usr/bin/env python3
"""Active-active, phase 4 (ADR-078): builds with two replicas -- one owner per job, numbers used once,
every build listed everywhere, build requests sent to the owner.

    python3 lab/aa/phase4_probe.py

Needs lab/aa/phase3.yaml applied with a controller of netci16 or later. Each build records which
replica ran it (the controller's HOSTNAME is its pod). Experiments:
  P1 owner    3 builds on r0: numbers 1, 2, 3; r1 lists each, and how long that took
  P2 routing  "Build now" on r1 for r0's job: r1 makes it on r0 for the same person; it runs on r0, once
  P3 cron     a job triggered every minute, on both replicas: one build per minute, none twice
  P4 owner    r0's pod killed: when can r1 build the job, and does it continue the numbers?
Evidence: lab/evidence/aa-phase4-<timestamp>.json.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import phase3_probe as p3  # noqa: E402  (clients pinned to one replica)

NS = p3.NS
HOMESYNC = "127.0.0.1:18080"  # a port-forward to netci-homesync, opened by main()
SCRIPT = "echo 'built'"
JOB_XML = ("<?xml version='1.1' encoding='UTF-8'?><flow-definition plugin=\"workflow-job\"><description></description>"
           "<keepDependencies>false</keepDependencies><properties>{props}</properties>"
           "<definition class=\"org.jenkinsci.plugins.workflow.cps.CpsFlowDefinition\" plugin=\"workflow-cps\">"
           "<script>{script}</script><sandbox>true</sandbox></definition><triggers/><disabled>false</disabled></flow-definition>")
CRON = ("<org.jenkinsci.plugins.workflow.job.properties.PipelineTriggersJobProperty><triggers><hudson.triggers.TimerTrigger>"
        "<spec>* * * * *</spec></hudson.triggers.TimerTrigger></triggers></org.jenkinsci.plugins.workflow.job.properties.PipelineTriggersJobProperty>")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: ANN002 - stdlib signature
        return None


def create(c: p3.R, name: str, props: str = "") -> int:
    xml = JOB_XML.format(props=props, script=SCRIPT.replace("<", "&lt;"))
    return c.req(f"/createItem?name={urllib.parse.quote(name)}", xml.encode(), "application/xml")[0]


def builds(c: p3.R, name: str) -> list[dict]:
    code, body, _ = c.req(f"/job/{urllib.parse.quote(name)}/api/json?tree=builds[number,result,building]")
    return json.loads(body)["builds"] if code == 200 else []


def by(c: p3.R, name: str, number: int) -> str | None:
    """Which replica ran a build: the one that published it, as netci-homesync recorded it from that
    replica's credential (a Pipeline's env has no HOSTNAME; the first run printed null)."""
    token = (p3.p0.STATE / "homesync" / "jenkins-0").read_text().strip()
    url = (f"http://{HOMESYNC}/v1/items?path=" + urllib.parse.quote(f"@builds/{name}/{number}"))
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.headers.get("X-Updated-By")
    except (urllib.error.URLError, OSError):
        return None


def trigger(c: p3.R, name: str) -> tuple[int, dict]:
    """POST build without following redirects: (status, headers)."""
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(c.jar), NoRedirect())
    if c.crumb is None:
        c.req("/crumbIssuer/api/json")
        crumb = json.loads(c.req("/crumbIssuer/api/json")[1])
        c.crumb = (crumb["crumbRequestField"], crumb["crumb"])
    r = urllib.request.Request(p3.VIP + f"/job/{urllib.parse.quote(name)}/build", data=b"", method="POST")
    r.add_header("Authorization", c.auth)
    r.add_header(*c.crumb)
    try:
        with opener.open(r, timeout=15) as resp:
            return resp.status, dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers)


def wait_finished(c: p3.R, name: str, count: int, timeout: float = 120) -> list[dict]:
    end = time.time() + timeout
    while time.time() < end:
        done = [b for b in builds(c, name) if not b["building"] and b["result"]]
        if len(done) >= count:
            return sorted(done, key=lambda b: b["number"])
        time.sleep(0.5)
    return sorted([b for b in builds(c, name) if not b["building"]], key=lambda b: b["number"])


def main() -> int:
    started = dt.datetime.now(dt.timezone.utc)
    tag = started.strftime("%H%M%S")
    import subprocess
    fwd = subprocess.Popen(["kubectl", "-n", NS, "port-forward", "svc/homesync", "18080:8080"], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, env={"KUBECONFIG": str(p3.p0.STATE / "kubeconfig"),
                                                           "PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(Path.home())})
    time.sleep(3)
    facts: dict = {"scenario": "aa-phase4", "started": started.isoformat(timespec="seconds")}
    r0, r1 = p3.R("r0"), p3.R("r1")

    # P1: three builds on the owner, listed on the other replica.
    name = f"p4-{tag}"
    assert create(r0, name) == 200
    p3.until(lambda: r1.job(name) is not None, 30)
    lags = []
    for _ in range(3):
        code, _ = trigger(r0, name)
        n = len(wait_finished(r0, name, len(lags) + 1))
        t = time.time()
        seen = p3.until(lambda: any(b["number"] == n for b in builds(r1, name)), 60, 0.1)
        lags.append(round((time.time() - t) * 1000) if seen is not None else None)
    on_r0 = wait_finished(r0, name, 3)
    facts["P1"] = {"numbers": [b["number"] for b in on_r0], "results": [b["result"] for b in on_r0],
                   "builtBy": [by(r0, name, b["number"]) for b in on_r0],
                   "listedOnR1": [b["number"] for b in sorted(builds(r1, name), key=lambda b: b["number"])],
                   "r1ConsoleOfBuild1": by(r1, name, 1), "listingLagMs": lags}
    print("P1", facts["P1"], flush=True)

    # P2: "Build now" on r1 for a job r0 owns, with r1's own crumb, as a browser on r1 sends it.
    # r1 makes the request on r0 for the same person (ADR-078): no redirect for the browser to follow.
    browser = p3.R("r1")
    code, headers = trigger(browser, name)
    low = {k.lower(): v for k, v in headers.items()}
    done = wait_finished(r0, name, 4)
    p2: dict = {"status": code, "builtOnHeader": low.get("x-netci-built-on"), "location": low.get("location"),
                "number": done[-1]["number"] if done else None, "builtBy": by(r0, name, done[-1]["number"]) if done else None,
                "notAlsoBuiltOnR1": all(by(r0, name, b["number"]) == "jenkins-0" for b in done)}
    facts["P2"] = p2
    print("P2", p2, flush=True)

    # P3: a cron job on both replicas.
    cron = f"p4cron-{tag}"
    assert create(r0, cron, CRON) == 200
    p3.until(lambda: r1.job(cron) is not None, 30)
    time.sleep(185)
    c0 = sorted(builds(r0, cron), key=lambda b: b["number"])
    c1 = sorted(builds(r1, cron), key=lambda b: b["number"])
    numbers = [b["number"] for b in c0]
    facts["P3"] = {"minutesWatched": 3, "buildsOnR0": numbers, "buildsOnR1": [b["number"] for b in c1],
                   "contiguous": numbers == list(range(1, len(numbers) + 1)),
                   "builtBy": sorted({by(r0, cron, n) or "?" for n in numbers}),
                   "oneOwnerBuiltThemAll": len({by(r0, cron, n) for n in numbers}) == 1}
    r0.req(f"/job/{cron}/disable", b"", "application/x-www-form-urlencoded")
    print("P3", facts["P3"], flush=True)

    # P4: the owner killed.
    before = max(b["number"] for b in builds(r0, name))
    p3.k("-n", NS, "delete", "pod", "jenkins-0", "--grace-period=0", "--force", check=False)
    t0 = time.time()
    attempts = []
    while time.time() - t0 < 90:
        code, headers = trigger(p3.R("r1"), name)
        attempts.append({"t": round(time.time() - t0, 1), "status": code})
        if code == 201:
            break
        time.sleep(2)
    done = wait_finished(r1, name, before + 1, 120) if attempts and attempts[-1]["status"] == 201 else []
    last = done[-1]["number"] if done else None
    facts["P4"] = {"lastNumberBefore": before, "attempts": attempts,
                   "firstAcceptedAfterSeconds": attempts[-1]["t"] if attempts and attempts[-1]["status"] == 201 else None,
                   "numberOnR1": last, "continued": last == before + 1, "builtBy": by(r1, name, last) if last else None}
    print("P4", facts["P4"], flush=True)
    p3.k("-n", NS, "rollout", "status", "statefulset/jenkins", "--timeout=600s", check=False)
    back = p3.R("r0")
    facts["P4"]["r0ListsR1sBuildAfterReturn"] = p3.until(lambda: any(b["number"] == last for b in builds(back, name)), 120, 1) is not None

    fwd.terminate()
    out = p3.p0.EVIDENCE / f"aa-phase4-{started.strftime('%Y%m%dT%H%M%SZ')}.json"
    out.write_text(json.dumps(facts, indent=2) + "\n")
    print("evidence:", out.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
