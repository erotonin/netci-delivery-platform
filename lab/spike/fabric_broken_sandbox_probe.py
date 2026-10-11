#!/usr/bin/env python3
"""Does the fabric keep a broken warm sandbox from costing a build its wait?

    python3 lab/spike/fabric_broken_sandbox_probe.py

1. A warm sandbox loses its readiness (its ready file removed, as a container that could not
   start again would): the fabric must fail it and replace it, and never hand it out.
2. Every warm sandbox of the standard pool is frozen (SIGSTOP of netci-sandbox, from its node): ready
   to Kubernetes, answering nothing. A fabric build must still finish: each claim of a frozen
   sandbox failed 15 s after it (WarmBindTimeout), the controller stopping at once (it asks what
   became of its claim) and asking again. Before, each such claim kept the build 300 s.
The frozen pods are deleted at the end whatever happens. Evidence:
lab/evidence/fabric-broken-sandbox-<timestamp>.json.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path

os.environ.setdefault("NETCI_CELL", "cell-b")
os.environ.setdefault("NETCI_CELL_PORT", "30081")
sys.path.insert(0, str(Path(__file__).parent))
import probe  # noqa: E402

ROOT = probe.ROOT
NS = "netci-agents"


def warm(pool: str) -> list[str]:
    out = probe.kubectl("-n", "netci-system", "exec", "netci-pg-0", "--", "psql", "-U", "netci", "-d", "netci", "-At", "-c",
                        f"SELECT pod FROM sandboxes WHERE pool = '{pool}' AND state = 'warm' ORDER BY created_at")
    return [p for p in out.split() if p]


def state_of(pod: str) -> str:
    return probe.kubectl("-n", "netci-system", "exec", "netci-pg-0", "--", "psql", "-U", "netci", "-d", "netci", "-At", "-c",
                         f"SELECT state || ' ' || coalesce(reason, '') FROM sandboxes WHERE pod = '{pod}'").strip()


def node_of(pod: str) -> str:
    return probe.kubectl("-n", NS, "get", "pod", pod, "-o", "jsonpath={.spec.nodeName}", check=False)


def on_node(node: str, cmd: str) -> str:
    i = node.rsplit("-", 1)[1]
    return subprocess.run([str(ROOT / "lab" / "vms.sh"), "ssh", i, cmd], capture_output=True, text=True, timeout=60).stdout.strip()


def freeze(pod: str) -> str:
    node = node_of(pod)
    pid = on_node(node, f"cid=$(sudo k3s crictl ps -q --label io.kubernetes.pod.name={pod} --label io.kubernetes.pod.namespace={NS} | head -1); "
                        "sudo k3s crictl inspect --output go-template --template '{{.info.pid}}' $cid")
    if not pid.isdigit():
        raise RuntimeError(f"{pod}: no container pid ({pid!r})")
    on_node(node, f"sudo kill -STOP {pid}")
    return f"{pod} on {node}, pid {pid}"


def fabric_log(since: str) -> list[str]:
    lead = probe.kubectl("-n", "netci-system", "get", "lease", "netci-fabric", "-o", "jsonpath={.spec.holderIdentity}")
    out = []
    for line in probe.kubectl("-n", "netci-system", "logs", lead, f"--since-time={since}").splitlines():
        if '"claimed"' in line or "sandbox failed" in line:
            d = json.loads(line)
            out.append(f'{d["time"][11:23]} {d["msg"]} {d.get("pod", "")} {d.get("reason", "")} warm={d.get("warm", "")}'.strip())
    return out


def main() -> int:
    started = dt.datetime.now(dt.timezone.utc)
    facts: dict = {"scenario": "fabric-broken-sandbox", "started": started.isoformat(timespec="seconds")}
    frozen: list[str] = []
    try:
        probe.wait("two warm standard sandboxes", lambda: len(warm("standard")) >= 2, 300, 2)
        # 1. Readiness lost.
        target = warm("standard")[0]
        t0 = time.monotonic()
        probe.kubectl("-n", NS, "exec", target, "--", "rm", "-f", "/tmp/netci-ready")
        probe.wait(f"{target} failed", lambda: state_of(target).split(" ")[0] in ("failed", "deleted"), 60, 0.5)
        facts["unready"] = {"pod": target, "failedAfterSeconds": round(time.monotonic() - t0, 1), "row": state_of(target)}
        probe.wait("the pool refilled", lambda: len(warm("standard")) >= 2 and target not in warm("standard"), 300, 2)
        # 2. Frozen: ready, answering nothing.
        for pod in warm("standard"):
            frozen.append(freeze(pod))
        facts["frozen"] = frozen
        since = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        j = probe.Jenkins()
        t1 = time.time()
        n = j.trigger("fabric-probe")
        b = probe.wait("the build done", lambda: (b := j.build("fabric-probe", n)) and not b["building"] and b.get("result") and b, 900, 1)
        facts["build"] = {"number": n, "result": b["result"], "triggerToDoneSeconds": round(time.time() - t1, 1)}
        time.sleep(3)
        facts["fabricLog"] = fabric_log(since)
        facts["frozenRows"] = {f.split()[0]: state_of(f.split()[0]) for f in frozen}
        ok = (facts["unready"]["row"].startswith(("failed", "deleted")) and b["result"] == "SUCCESS"
              and facts["build"]["triggerToDoneSeconds"] < 120)
        facts["verdict"] = "PASS" if ok else "FAIL"
    except Exception as e:  # noqa: BLE001 - recorded, and the frozen pods are removed below
        facts["verdict"], facts["error"] = "FAIL", str(e)
    finally:
        for f in frozen:
            probe.kubectl("-n", NS, "delete", "pod", f.split()[0], "--wait=false", check=False)
    out = probe.EVIDENCE / f"fabric-broken-sandbox-{started.strftime('%Y%m%dT%H%M%SZ')}.json"
    out.write_text(json.dumps(facts, indent=2) + "\n")
    print(json.dumps(facts, indent=1))
    print("evidence:", out.name)
    return 0 if facts["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
