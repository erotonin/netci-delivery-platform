#!/usr/bin/env python3
"""Lab probes for the cell agent (ADR-060): does holding the Lease really decide whether
Jenkins runs, on the real cluster?

    lab/spike/agent_probe.py handover     delete the pod: the old agent must release the Lease
                                          once Jenkins has exited, and the new pod take it at
                                          once instead of waiting out its duration
    lab/spike/agent_probe.py agent-restart SIGKILL the agent: the kubelet restarts it at once;
                                          the Lease never lapses, Jenkins must not be touched
    lab/spike/agent_probe.py agent-hang   SIGSTOP the agent: the guard must kill Jenkins within
                                          the renew deadline, and both must come back
    lab/spike/agent_probe.py partition    cut the pod off from the API server (iptables in its
                                          network namespace, the kubelet unaffected): the agent
                                          must kill Jenkins within the renew deadline, keep it
                                          dead while cut off, and start it again afterwards

Every run writes lab/evidence/agent-<scenario>-<timestamp>.json. Times that are compared with
each other come from one clock: the host's for Lease polling, the node's for log lines and the
moment of the fault (taken on the node in the same command that causes it).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / ".netci-gate" / "lab"
EVIDENCE = ROOT / "lab" / "evidence"
NODES = {"netci-lab-1": "192.168.122.211", "netci-lab-2": "192.168.122.212", "netci-lab-3": "192.168.122.213"}
NS = os.environ.get("NETCI_CELL", "cell-b")
DEADLINE = 4.0  # the agent's renew deadline (NETCI_RENEW_DEADLINE in lab/spike/cell.yaml)
os.environ["KUBECONFIG"] = str(STATE / "kubeconfig")


def kubectl(*args: str, check: bool = True, timeout: int = 60) -> str:
    r = subprocess.run(["kubectl", "-n", NS, *args], capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout


def node_ssh(node: str, command: str, timeout: int = 30) -> str:
    r = subprocess.run(
        ["ssh", "-i", str(STATE / "ssh" / "id_ed25519"), "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
         "-o", f"UserKnownHostsFile={STATE / 'ssh' / 'known_hosts'}", "-o", "StrictHostKeyChecking=yes",
         f"netci@{NODES[node]}", command], capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"ssh {node}: {r.stderr.strip()}")
    return r.stdout


def lease() -> tuple[str, int]:
    out = kubectl("get", "lease", "jenkins", "-o", "jsonpath={.spec.holderIdentity}|{.spec.leaseTransitions}")
    holder, _, epoch = out.partition("|")
    return holder, int(epoch or 0)


def pod() -> dict:
    return json.loads(kubectl("get", "pod", "jenkins-0", "-o", "json"))


def jenkins_status(p: dict) -> dict:
    for s in p["status"].get("containerStatuses", []):
        if s["name"] == "jenkins":
            return s
    return {}


def wait(what: str, predicate, timeout: float, interval: float = 0.2):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            v = predicate()
        except RuntimeError:
            v = None
        if v:
            return v
        time.sleep(interval)
    raise TimeoutError(what)


def json_lines(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        if line.startswith("{"):
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def ts(s: str) -> float:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def ready(p: dict) -> bool:
    return any(c["type"] == "Ready" and c["status"] == "True" for c in p["status"].get("conditions", []))


def scenario_handover(_args) -> dict:
    before = pod()
    old = f"jenkins-0/{before['metadata']['uid']}"
    holder, epoch = lease()
    assert holder == old, f"the running pod does not hold the lease: {holder!r}"
    follow = lambda c: subprocess.Popen(["kubectl", "-n", NS, "logs", "-f", "--timestamps", "jenkins-0", "-c", c, "--since=1s"],
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    logs, jlogs = follow("cell-agent"), follow("jenkins")
    time.sleep(1)
    t0 = time.monotonic()
    kubectl("delete", "pod", "jenkins-0", "--wait=false")
    released = wait("old holder releases", lambda: lease()[0] != old and time.monotonic(), 60)
    acquired = wait("new holder", lambda: (lambda h: h[0] not in ("", old) and (time.monotonic(), h))(lease()), 300)
    t_ready = wait("ready", lambda: ready(pod()) and time.monotonic(), 600, 0.5)
    logs.terminate()
    jlogs.terminate()
    # --timestamps prefixes each line with the kubelet's receive time (the node's clock, for
    # both containers): Jenkins' last line is when it stopped writing.
    stamped = lambda text: [(ts(l.split(" ", 1)[0]), l.split(" ", 1)[1] if " " in l else "") for l in text.splitlines() if l]
    agent_lines = stamped(logs.communicate(timeout=10)[0])
    jenkins_lines = stamped(jlogs.communicate(timeout=10)[0])
    old_log = json_lines("\n".join(text for _, text in agent_lines))
    t_released = next((t for t, text in agent_lines if '"lease released"' in text), None)
    t_jenkins_last = jenkins_lines[-1][0] if jenkins_lines else None
    t_acq, (new_holder, new_epoch) = acquired
    released_line = next((l for l in old_log if l.get("msg") == "lease released"), None)
    facts = {
        "old_holder": old, "new_holder": new_holder, "epoch_before": epoch, "epoch_after": new_epoch,
        "old_agent_released_explicitly": released_line is not None,
        "seconds_to_release": round(released - t0, 2),
        "seconds_to_new_holder": round(t_acq - t0, 2),
        "seconds_to_ready": round(t_ready - t0, 2),
        "jenkins_last_line": jenkins_lines[-1][1][:160] if jenkins_lines else None,
        "seconds_jenkins_last_line_to_release": round(t_released - t_jenkins_last, 3) if t_released and t_jenkins_last else None,
    }
    # The release must come after Jenkins stopped: released earlier, a successor could start
    # while this controller is still writing.
    ordered = t_released is not None and t_jenkins_last is not None and t_released >= t_jenkins_last
    facts["verdict"] = "PASS" if released_line and ordered and new_epoch == epoch + 1 else "FAIL"
    return facts


AGENT_PID = r"""for d in /proc/[0-9]*; do
  c=$(tr '\000' ' ' < "$d/cmdline" 2>/dev/null) || continue
  [ "$c" = "/cell-agent " ] && echo "${d#/proc/}"
done; true"""


def container_status(p: dict, name: str) -> dict:
    for s in p["status"].get("containerStatuses", []) + p["status"].get("initContainerStatuses", []):
        if s["name"] == name:
            return s
    return {}


def signal_agent(sig: str) -> tuple[float, dict]:
    """Sends sig to the agent from the controller container; returns the node-clock time."""
    p = pod()
    pids = kubectl("exec", "jenkins-0", "-c", "jenkins", "--", "sh", "-c", AGENT_PID).split()
    assert len(pids) == 1, f"expected one agent process, found {pids}"
    t = float(kubectl("exec", "jenkins-0", "-c", "jenkins", "--", "sh", "-c", f"date +%s.%N; kill -{sig} {pids[0]}").split()[0])
    return t, p


def scenario_agent_restart(_args) -> dict:
    """SIGKILL: the kubelet restarts the sidecar at once. The Lease never lapses, so Jenkins
    must not be interrupted."""
    holder, epoch = lease()
    t_kill, before = signal_agent("KILL")
    agent_restarts = container_status(before, "cell-agent").get("restartCount", 0)
    jenkins_restarts = container_status(before, "jenkins").get("restartCount", 0)
    wait("agent restarted", lambda: container_status(pod(), "cell-agent").get("restartCount", 0) > agent_restarts, 120)
    time.sleep(15)  # several deadlines
    after = pod()
    facts = {
        "agent_restarted": container_status(after, "cell-agent").get("restartCount", 0) - agent_restarts,
        "jenkins_restarts": container_status(after, "jenkins").get("restartCount", 0) - jenkins_restarts,
        "holder_unchanged": lease()[0] == holder, "epoch_before": epoch, "epoch_after": lease()[1],
        "ready": ready(after),
    }
    ok = facts["agent_restarted"] >= 1 and facts["jenkins_restarts"] == 0 and facts["holder_unchanged"] \
        and facts["epoch_after"] == epoch and facts["ready"]
    facts["verdict"] = "PASS" if ok else "FAIL"
    return facts


def scenario_agent_hang(_args) -> dict:
    """SIGSTOP: the agent is alive but renews nothing. The guard must kill Jenkins within the
    renew deadline of the last renewal; the agent's liveness probe then restarts it."""
    t_stop, before = signal_agent("STOP")
    restarts = container_status(before, "jenkins").get("restartCount", 0)
    after = wait("jenkins killed", lambda: (lambda s: s.get("restartCount", 0) > restarts and s)(container_status(pod(), "jenkins")), 120)
    guard = [l for l in json_lines(kubectl("logs", "jenkins-0", "-c", "jenkins", "--previous", "--tail=200"))
             if l.get("component") == "guard"]
    killed = next((l for l in guard if l.get("msg", "").startswith("gate closed or stale")), None)
    t_ready = wait("ready again", lambda: ready(pod()) and time.monotonic(), 600, 0.5)
    term = after.get("lastState", {}).get("terminated", {})
    facts = {
        "guard_killed_jenkins": killed is not None,
        "seconds_stop_to_jenkins_killed": round(ts(killed["time"]) - t_stop, 2) if killed else None,
        "jenkins_exit_code": term.get("exitCode"),
        "agent_restarted_by_liveness": container_status(pod(), "cell-agent").get("restartCount", 0)
        > container_status(before, "cell-agent").get("restartCount", 0),
        "holder_after": lease()[0], "epoch_after": lease()[1], "ready_again": bool(t_ready),
    }
    # The last renewal started at most one interval before the stop; the guard looks every
    # 250 ms: the kill lands between deadline - interval and deadline + 0.25 s after the stop.
    ok = killed and facts["seconds_stop_to_jenkins_killed"] <= DEADLINE + 0.5 and term.get("exitCode") == 137
    facts["verdict"] = "PASS" if ok else "FAIL"
    return facts


JAVA_OF_POD = r"""for p in $(pgrep -f jenkins.war); do
  grep -qE '{uid}|{uid_}' /proc/$p/cgroup 2>/dev/null && echo $p
done; true"""


def scenario_partition(args) -> dict:
    p = pod()
    node, uid = p["spec"]["nodeName"], p["metadata"]["uid"]
    epoch = lease()[1]
    restarts = jenkins_status(p).get("restartCount", 0)
    api = kubectl("get", "svc", "kubernetes", "-n", "default", "-o", "jsonpath={.spec.clusterIP}")
    # The agent container's network namespace is the pod's: dropping its traffic to the API
    # server's Service cuts the pod off while the kubelet, on the host's namespace, is unaffected.
    netns = (f"pid=$(sudo crictl inspect --output go-template --template '{{{{.info.pid}}}}' "
             f"$(sudo crictl ps -q --name cell-agent --label io.kubernetes.pod.namespace={NS} | head -1))")
    rule = f"OUTPUT -d {api} -p tcp --dport 443 -j DROP"
    t_cut = float(node_ssh(node, f"set -e; {netns}; date +%s.%N; sudo nsenter -t $pid -n iptables -I {rule}").split()[0])
    try:
        time.sleep(args.seconds)
        agent = json_lines(kubectl("logs", "jenkins-0", "-c", "cell-agent", "--since=5m"))
        lost = next((l for l in agent if l.get("msg", "").startswith("gate closed and controller killed")
                     and ts(l["time"]) >= t_cut), None)
        # While cut off, no Jenkins JVM of this pod may be running. Asked of the node, which is
        # not cut off: JVMs whose cgroup names this pod's UID (with "-" or "_", per cgroup driver).
        java_during = bool(node_ssh(node, JAVA_OF_POD.format(uid=uid, uid_=uid.replace("-", "_"))).split())
    finally:
        t_heal = float(node_ssh(node, f"set -e; {netns}; date +%s.%N; sudo nsenter -t $pid -n iptables -D {rule}").split()[0])
    t_ready = wait("ready again", lambda: ready(pod()) and time.monotonic(), 600, 0.5)
    t_ready_wall = time.time()
    holder, epoch_after = lease()
    facts = {
        "node": node, "cut_for_seconds": round(t_heal - t_cut, 1),
        "agent_reported_loss": lost is not None,
        "seconds_cut_to_jenkins_killed": round(ts(lost["time"]) - t_cut, 2) if lost else None,
        "jenkins_running_while_cut_off": java_during,
        "jenkins_restarts": jenkins_status(pod()).get("restartCount", 0) - restarts,
        "holder_after": holder, "epoch_before": epoch, "epoch_after": epoch_after,
        "ready_again_at": dt.datetime.fromtimestamp(t_ready_wall, dt.timezone.utc).isoformat(),
    }
    ok = lost and facts["seconds_cut_to_jenkins_killed"] <= DEADLINE + 0.5 and not java_during and t_ready
    facts["verdict"] = "PASS" if ok else "FAIL"
    return facts


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="scenario", required=True)
    sub.add_parser("handover")
    sub.add_parser("agent-restart")
    sub.add_parser("agent-hang")
    part = sub.add_parser("partition")
    part.add_argument("--seconds", type=float, default=20)
    args = ap.parse_args()
    run = {"handover": scenario_handover, "agent-restart": scenario_agent_restart, "agent-hang": scenario_agent_hang, "partition": scenario_partition}[args.scenario]
    started = dt.datetime.now(dt.timezone.utc)
    facts = run(args)
    record = {"scenario": f"agent-{args.scenario}", "cell": NS, "started": started.isoformat(), **facts}
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE / f"agent-{args.scenario}-{started.strftime('%Y%m%dT%H%M%SZ')}.json"
    path.write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps(record, indent=2))
    return 0 if facts["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
