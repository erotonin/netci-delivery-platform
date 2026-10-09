#!/usr/bin/env python3
"""Does upgrading netCI lose work? An organisation upgrades the platform and its cells while
people build: this does the same, with the lab's own install script, and checks what was lost.

    python3 lab/spike/upgrade_probe.py --to 172.17.0.1:8930/netci/netci@sha256:<digest> [--every 15]

While `lab/helm-install.sh` rolls every netCI component to --to (the supervisor, queue and fabric
replicas, then each cell's controller pod, whose cell agent and guard come from that image):
  - a 240 s `once-probe` build runs on cell-b, inside a netciOnce block: it must finish SUCCESS,
    with one marker and never refused;
  - a run is submitted through netci-queue every --every seconds: every one accepted must run
    exactly once, and the intake's refusals and timeouts are counted (its availability);
  - both cells' UIs are polled twice a second: how long each was down;
  - the supervisor must not act: no Fenced, PoweringOff, PodRestarted, FenceAborted or
    SupervisorAlert event, no out-of-service taint. A planned restart is not a failure.
Evidence: lab/evidence/upgrade-<timestamp>.json.
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
import urllib.error
import urllib.request
from pathlib import Path

os.environ.setdefault("NETCI_CELL", "cell-b")
os.environ.setdefault("NETCI_CELL_PORT", "30081")
sys.path.insert(0, str(Path(__file__).parent))
import probe  # noqa: E402
import queue_crash_probe as qp  # noqa: E402  (intake, builds, run_id_of)

ROOT = probe.ROOT
CELLS = {"cell-a": 30080, "cell-b": 30081}
ACTIONS = {"Fenced", "PoweringOff", "PodRestarted", "FenceAborted", "FenceFailed", "SupervisorAlert", "Unfenced"}


class UpPoller(threading.Thread):
    """Polls a cell's /login on every node's NodePort: the windows in which no node answered."""

    def __init__(self, port: int) -> None:
        super().__init__(daemon=True)
        self.port, self.stop, self.down, self._since = port, threading.Event(), [], None

    def up(self) -> bool:
        for ip in probe.NODES.values():
            try:
                with urllib.request.urlopen(f"http://{ip}:{self.port}/login", timeout=1) as r:
                    if r.status == 200:
                        return True
            except Exception:  # noqa: BLE001 - down, or that node's proxy is
                continue
        return False

    def run(self) -> None:
        while not self.stop.is_set():
            t, ok = time.monotonic(), self.up()
            if not ok and self._since is None:
                self._since = t
            elif ok and self._since is not None:
                self.down.append(round(t - self._since, 1))
                self._since = None
            time.sleep(0.5)
        if self._since is not None:
            self.down.append(round(time.monotonic() - self._since, 1))


class Submitter(threading.Thread):
    """A run through netci-queue every `every` seconds, as webhooks and schedules would."""

    def __init__(self, every: float, tag: str) -> None:
        super().__init__(daemon=True)
        self.every, self.tag, self.stop = every, tag, threading.Event()
        self.accepted, self.failed = [], []

    def run(self) -> None:
        i = 0
        while not self.stop.is_set():
            t0 = time.monotonic()
            try:
                r = qp.intake("POST", "/v1/runs", {"job": qp.JOB, "idempotencyKey": f"upgrade-{self.tag}-{i}"})
                self.accepted.append({"id": r["id"], "seconds": round(time.monotonic() - t0, 2)})
            except Exception as e:  # noqa: BLE001 - recorded: the intake's availability
                self.failed.append({"at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "error": str(e)[:200]})
            i += 1
            self.stop.wait(self.every)


def supervisor_events(since: str) -> list[str]:
    out = []
    for e in json.loads(probe.kubectl("get", "events", "-A", "-o", "json", check=False) or '{"items":[]}')["items"]:
        when = e.get("lastTimestamp") or e.get("eventTime") or ""
        if e.get("reason") in ACTIONS and when >= since:
            out.append(f'{when} {e["involvedObject"].get("namespace", "")}/{e["involvedObject"]["name"]} {e["reason"]}: {e.get("message", "")[:160]}')
    return out


def taints() -> list[str]:
    nodes = json.loads(probe.kubectl("get", "nodes", "-o", "json"))["items"]
    return [n["metadata"]["name"] for n in nodes if any(t["key"] == "node.kubernetes.io/out-of-service"
                                                        for t in n["spec"].get("taints") or [])]


def images() -> dict:
    out = {}
    for ns, kind, name in (("netci-system", "deploy", "netci-supervisor"), ("netci-system", "deploy", "netci-queue"),
                           ("netci-system", "deploy", "netci-fabric"), ("cell-a", "statefulset", "jenkins"),
                           ("cell-b", "statefulset", "jenkins")):
        spec = json.loads(probe.kubectl("-n", ns, "get", kind, name, "-o", "json"))["spec"]["template"]["spec"]
        imgs = {c["image"] for c in spec.get("containers", []) + spec.get("initContainers", []) if "/netci/netci@" in c["image"]}
        out[f"{ns}/{name}"] = sorted(i.split("@")[1][:19] for i in imgs)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--to", required=True, help="the netCI image to upgrade to, repository@sha256:...")
    ap.add_argument("--every", type=float, default=15)
    ap.add_argument("--seconds", type=int, default=240)
    args = ap.parse_args()
    if "@sha256:" not in args.to:
        print("--to must be pinned by digest", file=sys.stderr)
        return 2
    started = dt.datetime.now(dt.timezone.utc)
    since = started.strftime("%Y-%m-%dT%H:%M:%SZ")
    tag = started.strftime("%Y%m%dT%H%M%S")
    facts: dict = {"scenario": "upgrade", "to": args.to, "started": started.isoformat(timespec="seconds"), "imagesBefore": images()}

    j = probe.Jenkins()
    number = j.trigger("once-probe", {"DURATION": str(args.seconds)})
    facts["build"] = number
    probe.wait("the build to tick 20", lambda: len(probe.ticks(j.console("once-probe", number))) >= 20, 300, 0.5)
    before = {b["number"] for b in qp.builds(j)}

    pollers = {c: UpPoller(p) for c, p in CELLS.items()}
    sub = Submitter(args.every, tag)
    for t in (*pollers.values(), sub):
        t.start()
    t0 = time.monotonic()
    env = {**os.environ, "MONITORING": "on", "FENCE": "redfish", "NETCI_IMAGE": args.to}
    inst = subprocess.run([str(ROOT / "lab" / "helm-install.sh")], env=env, capture_output=True, text=True, timeout=3600)
    facts["installSeconds"] = round(time.monotonic() - t0, 1)
    facts["installExit"] = inst.returncode
    if inst.returncode != 0:
        facts["installError"] = inst.stderr.strip()[-800:]
    time.sleep(30)  # runs submitted just before the end of the rollout are given time to dispatch
    sub.stop.set()
    sub.join()

    j.after_restart()
    final = probe.wait("the build to finish", lambda: (b := j.build("once-probe", number)) and not b["building"] and b,
                       args.seconds + 900, 2)
    text = j.console("once-probe", number)
    markers = probe.kubectl("-n", "netci-system", "exec", "netci-pg-0", "--", "psql", "-U", "netci", "-d", "netci", "-tAc",
                            f"SELECT count(*) FROM once_markers WHERE client = 'cell-b' AND key = 'deploy' "
                            f"AND scope LIKE '%/once-probe#{number}'")
    facts["buildResult"] = final["result"]
    facts["once"] = {"markers": int(markers.strip() or 0), "refused": "already started in this build" in text}
    facts["ticksMissing"] = sorted(set(range(1, args.seconds + 1)) - set(probe.ticks(text)))[:20]

    ids = [a["id"] for a in sub.accepted]

    def all_finished():
        states = [qp.intake("GET", f"/v1/runs/{i}") for i in ids]
        return states if all(s["state"] in ("finished", "cancelled", "refused") for s in states) else None
    states = probe.wait("every accepted run finished", all_finished, 900, 3)
    for p in pollers.values():
        p.stop.set()
        p.join()
    j.after_restart()
    after = [b for b in qp.builds(j) if b["number"] not in before]
    per_run = {i: sum(1 for b in after if qp.run_id_of(b) == i) for i in ids}
    facts["runs"] = {"accepted": len(ids), "intakeFailures": sub.failed,
                     "intakeSecondsMax": max((a["seconds"] for a in sub.accepted), default=None),
                     "finishedSuccess": sum(1 for s in states if s["state"] == "finished" and s.get("result") == "SUCCESS"),
                     "notExactlyOnce": {i: n for i, n in per_run.items() if n != 1}}
    facts["cellDownSeconds"] = {c: p.down for c, p in pollers.items()}
    facts["supervisorActions"] = supervisor_events(since)
    facts["outOfService"] = taints()
    facts["imagesAfter"] = images()
    want = args.to.split("@")[1][:19]
    ok = (inst.returncode == 0 and final["result"] == "SUCCESS" and facts["once"]["markers"] == 1
          and not facts["once"]["refused"] and not facts["runs"]["notExactlyOnce"]
          and facts["runs"]["finishedSuccess"] == len(ids) and not facts["supervisorActions"]
          and not facts["outOfService"] and all(v == [want] for v in facts["imagesAfter"].values()))
    facts["verdict"] = "PASS" if ok else "FAIL"
    out = probe.EVIDENCE / f"upgrade-{started.strftime('%Y%m%dT%H%M%SZ')}.json"
    out.write_text(json.dumps(facts, indent=2) + "\n")
    print(json.dumps({k: v for k, v in facts.items() if k not in ("imagesBefore",)}, indent=1))
    print("evidence:", out.name)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
