#!/usr/bin/env python3
"""Spike probes for ADR-060: does a running Pipeline build survive the loss of its controller,
and how long does each phase of a takeover take?

    lab/spike/probe.py resume --failure jvm-kill|pod-delete|node-poweroff [--seconds 240]
    lab/spike/probe.py queue  --failure jvm-kill

Every run writes lab/evidence/spike-<scenario>-<timestamp>.json with the timings and the
facts observed; nothing is inferred that was not read back from Jenkins or the cluster.
"""

from __future__ import annotations

import argparse
import base64
import datetime
import http.cookiejar
import json
import os
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
NODES = {"netci-lab-1": "192.168.122.211", "netci-lab-2": "192.168.122.212", "netci-lab-3": "192.168.122.213"}
NS = os.environ.get("NETCI_CELL", "cell-a")
PORT = int(os.environ.get("NETCI_CELL_PORT", "30080"))
os.environ["KUBECONFIG"] = str(STATE / "kubeconfig")


def now() -> float:
    return time.monotonic()


_server = [0]  # index of the API server that answered last


def kubectl(*args: str, check: bool = True, timeout: int = 60) -> str:
    # Every node runs an API server, and the one this probe uses may be on the machine it just
    # powered off: try them in turn, each with a short request timeout. With one fixed server
    # the probe went blind for the whole outage and recorded 58 s for events that took 16-45 s.
    servers = list(NODES.values())
    out = None
    for k in range(len(servers)):
        i = (_server[0] + k) % len(servers)
        out = subprocess.run(["kubectl", "--request-timeout=3s", f"--server=https://{servers[i]}:6443", *args],
                             capture_output=True, text=True, timeout=timeout)
        unreachable = out.returncode != 0 and any(m in out.stderr for m in (
            "connect: no route to host", "i/o timeout", "connection refused", "Client.Timeout", "context deadline exceeded",
            "Unable to connect to the server"))
        if not unreachable:
            _server[0] = i
            break
    if check and out.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args)}: {out.stderr.strip()}")
    return out.stdout.strip()


class Jenkins:
    """Talks to the cell through any node's NodePort, so it keeps working when the controller
    moves. A fresh cookie jar and crumb after every restart: the old session is gone."""

    def __init__(self) -> None:
        self.password = (STATE / "cell-admin-password").read_text().strip()
        self._reset()

    def _reset(self) -> None:
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.crumb: tuple[str, str] | None = None

    def _request(self, path: str, data: bytes | None = None, timeout: float = 5) -> bytes:
        auth = base64.b64encode(f"admin:{self.password}".encode()).decode()
        last: Exception | None = None
        for ip in NODES.values():
            req = urllib.request.Request(f"http://{ip}:{PORT}{path}", data=data, method="POST" if data is not None else "GET")
            req.add_header("Authorization", f"Basic {auth}")
            if data is not None and self.crumb:
                req.add_header(*self.crumb)
            try:
                with self.opener.open(req, timeout=timeout) as resp:
                    return resp.read()
            except urllib.error.HTTPError:
                raise
            except OSError as exc:  # that node is gone; try the next one
                last = exc
        raise ConnectionError(str(last))

    def get_json(self, path: str) -> dict:
        return json.loads(self._request(path))

    def up(self) -> bool:
        try:
            self.get_json("/api/json?tree=mode")
            return True
        except Exception:  # noqa: BLE001 - any failure is "not up yet"
            return False

    def post(self, path: str, form: dict[str, str] | None = None) -> None:
        if self.crumb is None:
            c = self.get_json("/crumbIssuer/api/json")
            self.crumb = (c["crumbRequestField"], c["crumb"])
        self._request(path, data=urllib.parse.urlencode(form or {}).encode())

    def after_restart(self) -> None:
        self._reset()

    def trigger(self, job: str, params: dict[str, str] | None = None) -> int:
        before = self.get_json(f"/job/{job}/api/json?tree=nextBuildNumber")["nextBuildNumber"]
        self.post(f"/job/{job}/buildWithParameters" if params else f"/job/{job}/build", params)
        return before

    def build(self, job: str, number: int) -> dict:
        return self.get_json(f"/job/{job}/{number}/api/json?tree=building,result,duration")

    def console(self, job: str, number: int) -> str:
        return self._request(f"/job/{job}/{number}/consoleText", timeout=10).decode(errors="replace")

    def queue(self) -> list[dict]:
        return self.get_json("/queue/api/json?tree=items[id,task[name],why]")["items"]


def wait(what: str, predicate, timeout: float, interval: float = 0.5):
    deadline = now() + timeout
    while now() < deadline:
        try:
            value = predicate()
        except Exception:  # noqa: BLE001 - keep polling through restarts
            value = None
        if value:
            return value
        time.sleep(interval)
    raise TimeoutError(f"timed out waiting for {what}")


def controller_node() -> str:
    return kubectl("-n", NS, "get", "pod", "jenkins-0", "-o", "jsonpath={.spec.nodeName}")


def fail(kind: str, facts: dict) -> dict[str, float]:
    """Cause the failure; return timestamps of what the operator (later: the supervisor) did."""
    t: dict[str, float] = {"failure": now()}
    node = controller_node()
    facts["controllerNodeBefore"] = node
    if kind == "jvm-kill":
        pid = kubectl("-n", NS, "exec", "jenkins-0", "-c", "jenkins", "--", "sh", "-c", "pgrep -f jenkins.war | head -1")
        kubectl("-n", NS, "exec", "jenkins-0", "-c", "jenkins", "--", "kill", "-9", pid, check=False)
    elif kind == "pod-delete":
        kubectl("-n", NS, "delete", "pod", "jenkins-0", "--wait=false")
    elif kind == "node-poweroff":
        subprocess.run(["virsh", "-c", "qemu:///system", "destroy", node], check=True, capture_output=True)
        wait("node NotReady", lambda: kubectl("get", "node", node, "-o",
             "jsonpath={.status.conditions[?(@.type=='Ready')].status}") != "True", 180, 1)
        t["detected"] = now()
        # Fencing: the guest is confirmed off before the taint says so -- the taint's own rule.
        state = subprocess.run(["virsh", "-c", "qemu:///system", "domstate", node], capture_output=True, text=True).stdout.strip()
        if state != "shut off":
            raise RuntimeError(f"{node} is '{state}', not off: refusing to fence")
        kubectl("taint", "node", node, "node.kubernetes.io/out-of-service=nodeshutdown:NoExecute", "--overwrite")
        t["fenced"] = now()
    elif kind == "node-poweroff-supervised":
        # Only the fault. Detection, fencing and takeover are the Cell Supervisor's (ADR-060):
        # this records when each of its effects became visible, and touches nothing.
        holder = kubectl("-n", NS, "get", "lease", "jenkins", "-o", "jsonpath={.spec.holderIdentity}")
        facts["leaseHolderBefore"] = holder
        facts["supervisorBefore"] = supervisor_state()
        subprocess.run(["virsh", "-c", "qemu:///system", "destroy", node], check=True, capture_output=True)
        t["failure"] = now()
        # The wall-clock time too (the timings are monotonic): to line them up with the
        # supervisor's log and the cluster's events.
        facts["failureAt"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds")
        t["fenced"] = wait("supervisor fenced the node", lambda: "out-of-service" in kubectl(
            "get", "node", node, "-o", "jsonpath={.spec.taints[*].key}") and now(), 300, 0.2)
        t["leaseTaken"] = wait("a new pod holds the cell's lease", lambda: (lambda h: h and h != holder and now())(
            kubectl("-n", NS, "get", "lease", "jenkins", "-o", "jsonpath={.spec.holderIdentity}")), 300, 0.2)
        facts["supervisorAfter"] = supervisor_state()
    else:
        raise ValueError(kind)
    return t


def supervisor_state() -> dict:
    """Which supervisor replica leads, where, and its observation counters: a takeover slower
    than the policy allows is explained by these (a leader lost with the machine, or
    observations failing and starting over)."""
    leader = kubectl("-n", "netci-system", "get", "lease", "netci-supervisor", "-o", "jsonpath={.spec.holderIdentity}", check=False)
    state: dict = {"leader": leader}
    pods = json.loads(kubectl("-n", "netci-system", "get", "pods", "-l", "app=netci-supervisor", "-o", "json", check=False) or '{"items":[]}')
    for pod in pods["items"]:
        name = pod["metadata"]["name"]
        metrics = kubectl("get", "--raw", f"/api/v1/namespaces/netci-system/pods/{name}:9090/proxy/metrics", check=False, timeout=10)
        counters = {m: float(v) for m, v in re.findall(
            r"^netci_supervisor_(observe_errors_total|observation_resets_total|leading) (\S+)$", metrics, re.M)}
        state[name] = {"node": pod["spec"].get("nodeName"), **counters}
    return state


def supervisor_events() -> list[str]:
    out = kubectl("-n", NS, "get", "events", "-o",
                  "jsonpath={range .items[?(@.source.component=='netci-supervisor')]}{.lastTimestamp} {.reason}: {.message}{'\\n'}{end}", check=False)
    return [l for l in out.splitlines() if l][-10:]


def restore(kind: str, facts: dict) -> None:
    if kind == "node-poweroff-supervised":
        node = facts["controllerNodeBefore"]
        # The supervisor runs with auto power-on in the lab: it starts the machine once nothing
        # of a cell is left on it, and removes its taint once the node is Ready.
        try:
            wait("supervisor brought the node back", lambda: kubectl("get", "node", node, "-o",
                 "jsonpath={.status.conditions[?(@.type=='Ready')].status}") == "True" and "out-of-service" not in kubectl(
                 "get", "node", node, "-o", "jsonpath={.spec.taints[*].key}"), 600, 2)
            facts["nodeRestoredBySupervisor"] = True
        except TimeoutError:
            facts["nodeRestoredBySupervisor"] = False
            kind = "node-poweroff"  # clean up by hand, and say so
        facts["supervisorEvents"] = supervisor_events()
    if kind == "node-poweroff":
        node = facts["controllerNodeBefore"]
        subprocess.run(["virsh", "-c", "qemu:///system", "start", node], check=False, capture_output=True)
        wait("node Ready", lambda: kubectl("get", "node", node, "-o",
             "jsonpath={.status.conditions[?(@.type=='Ready')].status}") == "True", 300, 2)
        kubectl("taint", "node", node, "node.kubernetes.io/out-of-service-", check=False)


def ticks(text: str) -> list[int]:
    return [int(m) for m in re.findall(r"^tick (\d+) ", text, re.M)]


def scenario_resume(args) -> dict:
    j = Jenkins()
    job = args.job
    facts: dict = {"scenario": "resume", "failure": args.failure, "seconds": args.seconds, "job": job}
    number = j.trigger(job, {"DURATION": str(args.seconds)})
    facts["build"] = number
    wait(f"build to tick {args.crash_at_tick}", lambda: len(ticks(j.console(job, number))) >= args.crash_at_tick,
         300, 0.2)
    facts["ticksBeforeFailure"] = len(ticks(j.console(job, number)))
    # An agent on the controller's own node dies with it, and no takeover can resume a build
    # whose process is gone: record where it ran so the result is read correctly.
    facts["agentNode"] = kubectl("-n", NS, "get", "pods", "-o",
                                 "jsonpath={range .items[?(@.metadata.name!='jenkins-0')]}{.spec.nodeName}{end}")
    agent = kubectl("-n", NS, "get", "pods", "-o",
                    "jsonpath={range .items[?(@.metadata.name!='jenkins-0')]}{.metadata.name}{end}")
    facts["agentPod"] = agent
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    agent_log = EVIDENCE / f"agent-{agent}.log"
    # Followed from now until the pod goes: what the agent did while its controller was gone.
    subprocess.Popen(["kubectl", "-n", NS, "logs", "-f", agent], stdout=agent_log.open("w"), stderr=subprocess.STDOUT)
    facts["agentLog"] = str(agent_log.relative_to(ROOT))
    t = fail(args.failure, facts)
    j.after_restart()
    wait("Jenkins down", lambda: not j.up(), 60, 0.2) if not args.failure.startswith("node-poweroff") else None
    t["jenkinsUp"] = wait("Jenkins up", lambda: j.up() and now(), 900, 0.5)
    j.after_restart()
    facts["controllerNodeAfter"] = controller_node()
    base = len(ticks(j.console(job, number)))
    try:
        t["resumed"] = wait("new ticks after restart",
                            lambda: len(ticks(j.console(job, number))) > base and now(), 420, 1)
    except TimeoutError:
        facts["resumed"] = False
    final = wait("build to finish", lambda: (b := j.build(job, number)) and not b["building"] and b,
                 args.seconds + 900, 2)
    text = j.console(job, number)
    seen = ticks(text)
    facts.update({
        "result": final["result"],
        "reachedStageAfter": "reached the stage after the long step" in text,
        "ticksSeen": len(seen),
        "ticksMissing": sorted(set(range(1, args.seconds + 1)) - set(seen))[:20],
        "resumed": facts.get("resumed", True),
        "consoleTail": text.strip().splitlines()[-12:],
    })
    if job == "once-probe":
        # netciOnce (ADR-065): the block asked once, when it started; the resumed block did not
        # ask again and was not refused. One marker for this build, with one nonce.
        markers = kubectl("-n", "netci-system", "exec", "netci-pg-0", "--", "psql", "-U", "netci", "-d", "netci", "-tAc",
                          f"SELECT count(*) FROM once_markers WHERE client = '{NS}' AND key = 'deploy' "
                          f"AND scope LIKE '%/once-probe#{number}'")
        facts["once"] = {"markers": int(markers.strip() or 0),
                         "firstStarts": text.count("netciOnce('deploy'): first start in this build"),
                         "refused": "already started in this build" in text}
    start = t["failure"]
    facts["timings_s"] = {k: round(v - start, 1) for k, v in t.items() if k != "failure"}
    restore(args.failure, facts)
    return facts


def scenario_queue(args) -> dict:
    j = Jenkins()
    facts: dict = {"scenario": "queue", "failure": args.failure}
    j.trigger("queue-probe")
    wait("item in queue", lambda: any(i["task"]["name"] == "queue-probe" for i in j.queue()), 60, 1)
    facts["queuedBefore"] = [i["task"]["name"] for i in j.queue()]
    t = fail(args.failure, facts)
    j.after_restart()
    if args.failure != "node-poweroff":
        wait("Jenkins down", lambda: not j.up(), 60, 0.2)
    t["jenkinsUp"] = wait("Jenkins up", lambda: j.up() and now(), 900, 0.5)
    time.sleep(10)  # let the queue be reloaded if it is going to be
    facts["queuedAfter"] = [i["task"]["name"] for i in j.queue()]
    facts["queueSurvived"] = "queue-probe" in facts["queuedAfter"]
    facts["timings_s"] = {k: round(v - t["failure"], 1) for k, v in t.items() if k != "failure"}
    for item in j.get_json("/queue/api/json?tree=items[id]")["items"]:
        j.post(f"/queue/cancelItem?id={item['id']}")
    restore(args.failure, facts)
    return facts


def scenario_bench(args) -> dict:
    """What synchronous JENKINS_HOME writes cost a build: per-stage durations of io-probe
    (200 flow-node writes, then 20,000 log lines), median of N runs."""
    j = Jenkins()
    runs = []
    for _ in range(args.runs):
        number = j.trigger("io-probe")
        wait("io-probe to finish", lambda: (b := j.build("io-probe", number)) and not b["building"] and b, 900, 2)
        describe = j.get_json(f"/job/io-probe/{number}/wfapi/describe")
        runs.append({s["name"]: s["durationMillis"] for s in describe["stages"]} | {"total": describe["durationMillis"]})
    med = {k: sorted(r[k] for r in runs)[len(runs) // 2] for k in runs[0]}
    storage = kubectl("-n", NS, "get", "pvc", "home-jenkins-0", "-o", "jsonpath={.spec.storageClassName}")
    return {"scenario": "bench", "failure": storage, "storageClass": storage, "runs": runs, "median_ms": med}


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("resume")
    r.add_argument("--failure", required=True, choices=["jvm-kill", "pod-delete", "node-poweroff", "node-poweroff-supervised"])
    r.add_argument("--seconds", type=int, default=240)
    r.add_argument("--job", default="resume-probe", choices=["resume-probe", "once-probe"])
    # How far into the step to fail. 1 is the worst case for state written just before: the
    # step's start may not have reached the disk yet.
    r.add_argument("--crash-at-tick", type=int, default=20)
    q = sub.add_parser("queue")
    q.add_argument("--failure", required=True, choices=["jvm-kill", "pod-delete", "node-poweroff"])
    b = sub.add_parser("bench")
    b.add_argument("--runs", type=int, default=3)
    args = parser.parse_args()
    started = time.strftime("%Y%m%dT%H%M%S")
    facts = {"resume": scenario_resume, "queue": scenario_queue, "bench": scenario_bench}[args.cmd](args)
    facts["startedAt"] = started
    facts["jenkinsVersion"] = Jenkins().get_json("/api/json?tree=mode") and "2.555.3"
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    out = EVIDENCE / f"spike-{args.cmd}-{facts['failure']}-{started}.json"
    out.write_text(json.dumps(facts, indent=2) + "\n")
    print(json.dumps({k: v for k, v in facts.items() if k != "consoleTail"}, indent=2))
    print(f"evidence: {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
