#!/usr/bin/env python3
"""Active-active, phase 5, by hand: can a surviving replica continue a Pipeline that was running on a
replica that died -- without restarting itself? The mechanism, proven before any code automates it.

    python3 lab/aa/phase5_manual.py

1. `long` (90 ticks on an agent, max survivability) started on r0; agent-0 runs it.
2. At tick 15, r0's copy of the running build is copied into r1's home (what replication will do).
3. r0 is killed, with its machine cordoned so it stays down (a dead machine; fencing done).
4. agent-0 reconnects through the VIP; r0 being down, the balancer gives it to r1.
5. On r1, through the script console: the build's number made known, the run loaded, its execution
   resumed -- what Jenkins does for each run at start, for this run only.
6. Watched: does the build finish on r1, and is each tick in its log once?
Evidence: lab/evidence/aa-phase5-manual-<timestamp>.json. r0's machine is uncordoned at the end
after r0's stale copy of the build is replaced by the finished one.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import phase3_probe as p3  # noqa: E402
import phase4_probe as p4  # noqa: E402

NS = p3.NS
# Loading the run is enough: it resumes its execution itself, as at a Jenkins start. The first
# version also called onResume() on its steps -- a second resumption, "Already resumed", which made
# the sh step interrupt its process and fail the build (aa-phase5-manual-20261013T164147Z.json).
ADOPT = """
def job = jenkins.model.Jenkins.get().getItemByFullName('long')
job.getLazyBuildMixIn()._getRuns().recognizeNumber(%d)
def run = job.getBuildByNumber(%d)
print("adopted #" + run.number + ": building=" + run.isBuilding())
"""


def script(c: p3.R, text: str) -> str:
    return c.req("/scriptText", urllib.parse.urlencode({"script": text}).encode(), "application/x-www-form-urlencoded")[1].strip()


def kubectl(*args: str, stdin: bytes | None = None) -> bytes:
    env = {"KUBECONFIG": str(p3.p0.STATE / "kubeconfig"), "PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(Path.home())}
    return subprocess.run(["kubectl", *args], input=stdin, capture_output=True, check=True, env=env).stdout


def console(c: p3.R, n: int) -> str:
    return c.req(f"/job/long/{n}/consoleText")[1]


def ticks(text: str) -> list[int]:
    return [int(m.group(1)) for m in re.finditer(r"^tick (\d+) on", text, re.M)]


def main() -> int:
    started = dt.datetime.now(dt.timezone.utc)
    facts: dict = {"scenario": "aa-phase5-manual", "started": started.isoformat(timespec="seconds")}
    r0 = p3.R("r0")
    node0 = kubectl("-n", NS, "get", "pod", "jenkins-0", "-o", "jsonpath={.spec.nodeName}").decode()
    code, _ = p4.trigger(r0, "long")
    assert code == 201, code
    n = p3.until(lambda: p4.builds(p3.R("r0"), "long") and p4.builds(p3.R("r0"), "long")[0]["building"], 60) and \
        p4.builds(p3.R("r0"), "long")[0]["number"]
    p3.until(lambda: max(ticks(console(p3.R("r0"), n)) or [0]) >= 15, 120, 0.5)
    facts["build"] = n

    # 2. The running build's directory, r0 -> r1 (replication stands in for this later).
    raw = kubectl("-n", NS, "exec", "jenkins-0", "-c", "jenkins", "--", "tar", "-C", "/var/jenkins_home/jobs/long/builds",
                  "-cf", "-", str(n))
    kubectl("-n", NS, "exec", "-i", "jenkins-1", "-c", "jenkins", "--", "sh", "-c",
            "mkdir -p /var/jenkins_home/jobs/long/builds && tar -C /var/jenkins_home/jobs/long/builds -xf -", stdin=raw)
    facts["copiedAtTick"] = max(ticks(console(p3.R("r0"), n)))

    # 3. r0 dies and stays down.
    kubectl("cordon", node0)
    t_dead = time.time()
    kubectl("-n", NS, "delete", "pod", "jenkins-0", "--grace-period=0", "--force")
    facts["killedAtTick"] = facts["copiedAtTick"]  # within a second of the copy

    # 4. agent-0 on r1.
    def agent0_on_r1() -> bool:
        body = p3.R("r1").req("/computer/agent-0/api/json?tree=offline")[1]
        return json.loads(body).get("offline") is False

    facts["agent0OnR1AfterSeconds"] = round(p3.until(agent0_on_r1, 120, 1) or -1, 1)
    print("agent-0 on r1 after", facts["agent0OnR1AfterSeconds"], "s", flush=True)

    # 5. Adopt on r1.
    out = script(p3.R("r1"), ADOPT % (n, n))
    facts["adoptedAfterSeconds"] = round(time.time() - t_dead, 1)
    facts["adoption"] = out
    print(out, flush=True)

    # 6. Watch it finish on r1.
    done = p3.until(lambda: json.loads(p3.R("r1").req(f"/job/long/{n}/api/json?tree=building,result")[1]).get("result") is not None, 240, 2)
    final = json.loads(p3.R("r1").req(f"/job/long/{n}/api/json?tree=building,result")[1])
    seen = ticks(console(p3.R("r1"), n))
    facts["finished"] = {"result": final.get("result"), "afterSeconds": round(done, 1) if done else None,
                         "ticksSeen": len(seen), "distinct": len(set(seen)), "missing": sorted(set(range(1, 91)) - set(seen)),
                         "duplicated": sorted({t for t in seen if seen.count(t) > 1}),
                         "hosts": sorted(set(re.findall(r"^tick \d+ on (\S+)", console(p3.R("r1"), n), re.M)))}
    print("finished", facts["finished"], flush=True)

    # r0's machine back, with its stale copy of the build replaced by the finished one first.
    pv = kubectl("-n", NS, "get", "pvc", "home-jenkins-0", "-o", "jsonpath={.spec.volumeName}").decode()
    path = kubectl("get", "pv", pv, "-o", "jsonpath={.spec.hostPath.path}{.spec.local.path}").decode()
    facts["r0HomeOnHost"] = path
    final_dir = kubectl("-n", NS, "exec", "jenkins-1", "-c", "jenkins", "--", "tar", "-C", "/var/jenkins_home/jobs/long/builds", "-cf", "-", str(n))
    vm = {"netci-lab-1": "1", "netci-lab-2": "2", "netci-lab-3": "3"}[node0]
    subprocess.run([str(p3.p0.ROOT / "lab" / "vms.sh"), "ssh", vm,
                    f"sudo rm -rf '{path}/jobs/long/builds/{n}' && sudo tar -C '{path}/jobs/long/builds' -xf - && sudo chown -R 1000:1000 '{path}/jobs/long/builds/{n}'"],
                   input=final_dir, check=True, capture_output=True)
    kubectl("uncordon", node0)
    out_file = p3.p0.EVIDENCE / f"aa-phase5-manual-{started.strftime('%Y%m%dT%H%M%SZ')}.json"
    out_file.write_text(json.dumps(facts, indent=2) + "\n")
    print("evidence:", out_file.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
