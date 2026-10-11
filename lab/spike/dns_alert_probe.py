#!/usr/bin/env python3
"""Does NetciClusterDNSOnOneMachine say when cluster DNS answers from one machine? End to end:
CoreDNS scaled to one replica, the supervisor's metric, the leader's log line, the alert pending
then firing after its 10 minutes, and cleared once two replicas run again (the probe restores
them whatever happens).

    python3 lab/spike/dns_alert_probe.py
Evidence: lab/evidence/cluster-dns-alert-<timestamp>.json.
"""
import datetime as dt
import json
import os
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
os.environ["KUBECONFIG"] = str(ROOT / ".netci-gate" / "lab" / "kubeconfig")
P = "http://192.168.122.212:30909"


def q(expr):
    u = P + "/api/v1/query?" + urllib.parse.urlencode({"query": expr})
    return [r["value"][1] for r in json.load(urllib.request.urlopen(u, timeout=10))["data"]["result"]]


def alert():
    for a in json.load(urllib.request.urlopen(P + "/api/v1/alerts", timeout=10))["data"]["alerts"]:
        if a["labels"]["alertname"] == "NetciClusterDNSOnOneMachine":
            return a["state"]
    return "inactive"


def k(*a):
    return subprocess.run(["kubectl", *a], capture_output=True, text=True).stdout


def main():
    started = dt.datetime.now(dt.timezone.utc)
    facts = {"scenario": "cluster-dns-alert", "started": started.isoformat(timespec="seconds"),
             "before": q("netci_supervisor_cluster_dns_machines")}
    k("-n", "kube-system", "scale", "deploy", "coredns", "--replicas=1")
    t0, seen = time.monotonic(), {}
    try:
        while time.monotonic() - t0 < 16 * 60:
            v, s = q("max(netci_supervisor_cluster_dns_machines)"), alert()
            if v == ["1"]:
                seen.setdefault("metric1AfterSeconds", round(time.monotonic() - t0))
            if s == "pending":
                seen.setdefault("pendingAfterSeconds", round(time.monotonic() - t0))
            if s == "firing":
                seen.setdefault("firingAfterSeconds", round(time.monotonic() - t0))
                break
            time.sleep(10)
    finally:
        k("-n", "kube-system", "scale", "deploy", "coredns", "--replicas=2")
    lead = k("-n", "netci-system", "get", "lease", "netci-supervisor", "-o", "jsonpath={.spec.holderIdentity}")
    facts["leaderLog"] = [line[:240] for line in k("-n", "netci-system", "logs", lead).splitlines() if "cluster DNS" in line][-1:]
    t1 = time.monotonic()
    while time.monotonic() - t1 < 300 and alert() != "inactive":
        time.sleep(10)
    facts.update(seen, clearedAfterSeconds=round(time.monotonic() - t1), after=q("netci_supervisor_cluster_dns_machines"))
    facts["verdict"] = "PASS" if ("firingAfterSeconds" in seen and facts["leaderLog"] and facts["after"]
                                  and all(v == "2" for v in facts["after"])) else "FAIL"
    out = ROOT / "lab" / "evidence" / f"cluster-dns-alert-{started.strftime('%Y%m%dT%H%M%SZ')}.json"
    out.write_text(json.dumps(facts, indent=2) + "\n")
    print(json.dumps(facts, indent=1))
    print("evidence:", out.name)


if __name__ == "__main__":
    main()
