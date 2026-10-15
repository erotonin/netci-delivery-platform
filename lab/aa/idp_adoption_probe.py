#!/usr/bin/env python3
"""Does the IDP's own build survive its replica's death (ADR-079 with the real pipeline, ADR-092)?

    set -a; source .netci-gate/aa/idp.env; set +a
    python3 lab/aa/idp_adoption_probe.py [--at build|vulnerability-scan] [--kill pod|machine]

ADR-079 showed a running build adopted by the surviving replica -- with a 90-tick shell loop. This
starts a real build-only run of payments-api through the IDP's API (signed in as dana; the IDP must
run with NETCI_AGENT_KIND=static, so the build is on the lab's inbound agents) and, when the run's
stage `--at` is running, kills the replica that owns the Jenkins build:
  pod      its pod deleted with no grace, left to Kubernetes
  machine  its machine's power cut (virsh destroy), powered on again afterwards
Nothing is done by hand afterwards. Recorded: which replica adopted the build and when, the IDP run's
result, every stage once, the digest, and whether its signature still verifies.
Evidence: lab/evidence/aa-idp-adoption-<timestamp>.json.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import idp_e2e as e  # noqa: E402
import phase3_probe as p3  # noqa: E402
import phase4_probe as p4  # noqa: E402

NS = p3.NS
VIRSH = ["virsh", "-c", "qemu:///system"]
REPLICA = {"r0": "jenkins-0", "r1": "jenkins-1"}
ROOT = Path(__file__).resolve().parents[2]


def holder(path: str) -> tuple[str | None, dict]:
    """The replica that has the build, asking each itself with redirects not followed (phase5_probe)."""
    for r in REPLICA:
        c = p3.R(r)
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(c.jar), p4.NoRedirect())
        req = urllib.request.Request(p3.VIP + path + "api/json?tree=building,result", headers={"Authorization": c.auth})
        try:
            with opener.open(req, timeout=3) as resp:
                if resp.headers.get("X-Netci-Replica") == r:
                    return r, json.loads(resp.read())
        except (urllib.error.URLError, OSError):
            continue
    return None, {}


def stages(token: str, run_id: str) -> dict[str, str]:
    code, body = e.call("GET", f"/pipeline-runs/{run_id}/stages", token)
    items = body.get("items", body) if isinstance(body, dict) else body
    return {s["stageId"]: s["status"] for s in items or []} if code == 200 else {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--at", default="build")
    ap.add_argument("--kill", choices=["pod", "machine"], default="pod")
    a = ap.parse_args()
    dev = e.token("NETCI_ACCEPTANCE_USER_DEVELOPER")
    code, refs = e.call("GET", f"/modules/{e.MODULE}/git-refs", dev)
    head = next(b["sha"] for b in refs["branches"] if b["name"] == "main")
    code, run = e.call("POST", f"/modules/{e.MODULE}/pipeline-runs", dev,
                       {"commitSha": head, "branch": "main", "environment": "dev", "deploy": False},
                       {"Idempotency-Key": f"ha-adopt-{head[:12]}-{int(time.time())}"})
    if code != 202:
        raise SystemExit(f"run not started: {code} {run}")
    rid = run["id"]
    print(f"run {rid[:8]} on {head[:7]}", flush=True)
    deadline = time.time() + 300
    while time.time() < deadline and stages(dev, rid).get(a.at) != "running":
        time.sleep(0.5)
    st = stages(dev, rid)
    if st.get(a.at) != "running":
        raise SystemExit(f"stage {a.at} never ran: {st}")
    page = e.call("GET", f"/pipeline-runs/{rid}", dev)[1].get("consoleUrl") or ""
    path = "/" + page.split("://", 1)[1].split("/", 1)[1] if page else ""
    if not path:
        raise SystemExit("the run has no Jenkins page to follow")
    owner, _ = holder(path)
    node = p3.k("-n", NS, "get", "pod", REPLICA[owner], "-o", "jsonpath={.spec.nodeName}")
    t0 = time.time()
    if a.kill == "pod":
        p3.k("-n", NS, "delete", "pod", REPLICA[owner], "--grace-period=0", "--force")
    else:
        subprocess.run([*VIRSH, "destroy", node], check=True, capture_output=True)
    print(f"[{a.kill}] {owner} ({REPLICA[owner]} on {node}) killed during {a.at}", flush=True)

    timeline, final = [], {}
    while time.time() - t0 < 900:
        h, state = holder(path)
        code, now = e.call("GET", f"/pipeline-runs/{rid}", dev)
        timeline.append({"t": round(time.time() - t0, 1), "holder": h, "building": state.get("building"),
                         "jenkinsResult": state.get("result"), "runStatus": now.get("status") if code == 200 else code})
        if code == 200 and now["status"] in ("succeeded", "failed", "cancelled"):
            final = now
            break
        time.sleep(1)
    adopted = [t for t in timeline if t["holder"] and t["holder"] != owner and t["building"]]
    st = stages(dev, rid)
    facts = {"run": rid, "commit": head, "killed": a.kill, "during": a.at, "owner": owner, "node": node,
             "adoptedBy": adopted[0]["holder"] if adopted else None,
             "adoptedAfterSeconds": adopted[0]["t"] if adopted else None,
             "runStatus": final.get("status"), "finishedAfterSeconds": timeline[-1]["t"] if final else None,
             "artifactDigest": final.get("artifactDigest"), "stages": st}
    # The adopting replica's own record of it, and every stage reported once.
    for pod in REPLICA.values():
        for line in p3.k("-n", NS, "logs", pod, "--since=20m", check=False).splitlines():
            if "adopted " in line and path.strip("/").split("/")[1] in line:
                facts["adoptionLogged"] = {"by": pod, "line": line[:200]}
    code, logs = e.call("GET", f"/pipeline-runs/{rid}/logs", dev)
    lines = [x.get("message", x) if isinstance(x, dict) else x for x in (logs.get("items") or logs.get("lines") or [])] if code == 200 else []
    facts["stageCallbacks"] = {s: sum(1 for l in lines if re.search(rf"stage {re.escape(s)}\b.*succeeded", str(l))) for s in st}
    if final.get("artifactDigest"):
        # What the IDP itself established about the artifact (signature, provenance, scan).
        code, ev = e.call("GET", f"/pipeline-runs/{rid}/security-evidence", dev)
        facts["securityEvidence"] = {"code": code, **({k: v for k, v in ev.items() if isinstance(v, (bool, str)) and len(str(v)) < 120}
                                                       if isinstance(ev, dict) else {})}
    if a.kill == "machine":
        subprocess.run([*VIRSH, "start", node], capture_output=True)
    facts["timeline"] = timeline
    out = ROOT / f"lab/evidence/aa-idp-adoption-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json"
    out.write_text(json.dumps(facts, indent=2) + "\n")
    print(json.dumps({k: v for k, v in facts.items() if k != "timeline"}, indent=1))
    print(f"evidence {out.relative_to(ROOT)}")
    return 0 if final.get("status") == "succeeded" and adopted else 1


if __name__ == "__main__":
    raise SystemExit(main())
