#!/usr/bin/env python3
"""Lab probe: a push to the lab GitLab reaches Jenkins through netCI's webhook intake.

    lab/spike/webhook_probe.py

The lab GitLab runs in Docker and cannot reach the libvirt network, so for the duration of
the probe:
  - a kubectl port-forward serves netci-queue on the host's docker0 address (172.17.0.1:30091);
  - that one address is added to GitLab's outbound allowlist.
It then pushes a commit to root/netci-webhook-probe (created once), waits for the run and the
Jenkins build, asks GitLab to resend the same delivery, and checks that this made no second
run. Whatever happens, it deletes the hook, restores the allowlist and stops the forward.
Evidence: lab/evidence/webhook-gitlab-<timestamp>.json.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / ".netci-gate" / "lab"
CORP = ROOT / ".netci-gate" / "corp"
EVIDENCE = ROOT / "lab" / "evidence"
os.environ.setdefault("NETCI_CELL", "cell-b")
os.environ.setdefault("NETCI_CELL_PORT", "30081")
os.environ["KUBECONFIG"] = str(STATE / "kubeconfig")
sys.path.insert(0, str(Path(__file__).parent))
import probe  # noqa: E402

GITLAB = "http://172.17.0.1:8929/api/v4"
FORWARD = "172.17.0.1:30091"
PROJECT = "root/netci-webhook-probe"


def gitlab(method: str, path: str, body: dict | None = None):
    token = (CORP / "gitlab_admin_token").read_text().strip()
    req = urllib.request.Request(GITLAB + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"PRIVATE-TOKEN": token, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read()
        return json.loads(data) if data else None


def psql(sql: str) -> str:
    return probe.kubectl("-n", "netci-system", "exec", "netci-pg-0", "--", "psql", "-U", "netci", "-d", "netci", "-At", "-c", sql)


def runs_for(sha: str) -> list[list[str]]:
    out = psql(f"SELECT id, state, coalesce(build_number, 0), coalesce(result, '') FROM runs "
               f"WHERE job = 'webhook-probe' AND parameters->>'GIT_SHA' = '{sha}' ORDER BY accepted_at")
    return [line.split("|") for line in out.splitlines() if line]


def wait(what, predicate, timeout, interval=1.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            v = predicate()
        except Exception:  # noqa: BLE001
            v = None
        if v:
            return v
        time.sleep(interval)
    raise TimeoutError(what)


def main() -> int:
    started = dt.datetime.now(dt.timezone.utc)
    facts: dict = {"scenario": "webhook-gitlab", "started": started.isoformat()}
    forward = subprocess.Popen(["kubectl", "-n", "netci-system", "port-forward", "--address", "172.17.0.1",
                                "svc/netci-queue", "30091:8080"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    settings = gitlab("GET", "/application/settings")
    allow_before = list(settings.get("outbound_local_requests_whitelist") or [])
    hook_id = project_id = None
    try:
        wait("port-forward", lambda: urllib.request.urlopen(f"http://{FORWARD}/healthz", timeout=2).status == 200, 30)
        gitlab("PUT", "/application/settings", {"outbound_local_requests_whitelist": allow_before + [FORWARD]})
        facts["gitlab_version"] = gitlab("GET", "/version")["version"]
        try:
            project = gitlab("GET", "/projects/" + urllib.parse.quote(PROJECT, safe=""))
        except urllib.error.HTTPError:
            project = gitlab("POST", "/projects", {"name": "netci-webhook-probe", "path": "netci-webhook-probe",
                                                   "initialize_with_readme": True, "visibility": "private"})
        project_id = project["id"]
        secret = (STATE / "queue" / "gitlab-hook-secret").read_text().strip()
        hook = gitlab("POST", f"/projects/{project_id}/hooks", {
            "url": f"http://{FORWARD}/v1/hooks/lab-gitlab", "token": secret, "push_events": True,
            "enable_ssl_verification": False})
        hook_id = hook["id"]

        stamp = started.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        commit = gitlab("POST", f"/projects/{project_id}/repository/commits", {
            "branch": "main", "commit_message": f"webhook probe {stamp}",
            "actions": [{"action": "create", "file_path": f"probes/{stamp}.txt", "content": stamp}]})
        sha = commit["id"]
        t_push = time.monotonic()
        facts["commit"] = sha

        run = wait("run accepted", lambda: runs_for(sha), 60)[0]
        facts["seconds_push_to_accepted"] = round(time.monotonic() - t_push, 2)
        done = wait("run finished", lambda: (lambda r: r if r[0][1] in ("finished", "refused", "cancelled") else None)(runs_for(sha)), 300)[0]
        facts["seconds_push_to_finished"] = round(time.monotonic() - t_push, 2)
        facts["run"] = {"id": done[0], "state": done[1], "build": int(done[2]), "result": done[3]}
        j = probe.Jenkins()
        console = j.console("webhook-probe", int(done[2])) if int(done[2]) else ""
        facts["jenkins_console_line"] = next((l for l in console.splitlines() if l.startswith("push of")), None)

        # Resend the same delivery: GitLab sends it again (same Idempotency-Key, by its docs).
        events = gitlab("GET", f"/projects/{project_id}/hooks/{hook_id}/events")
        push_events = [e for e in events if e.get("trigger") == "push_hooks"]
        facts["gitlab_delivery_status"] = push_events[0].get("response_status") if push_events else None
        facts["gitlab_delivery_response"] = (push_events[0].get("response_body") or "")[:300] if push_events else None
        if push_events:
            try:
                gitlab("POST", f"/projects/{project_id}/hooks/{hook_id}/events/{push_events[0]['id']}/resend")
                deliveries = wait("the resent delivery logged", lambda: (lambda ev: ev if len(ev) >= 2 else None)(
                    [e for e in gitlab("GET", f"/projects/{project_id}/hooks/{hook_id}/events") if e.get("trigger") == "push_hooks"]), 60)
                facts["resend"] = "sent"
                # What netCI answered each delivery: the same run both times proves the resend
                # reached it and was recognised, not merely that GitLab accepted the request.
                facts["deliveries"] = [{"status": e.get("response_status"), "response": (e.get("response_body") or "")[:300]}
                                       for e in deliveries]
            except urllib.error.HTTPError as e:
                facts["resend"] = f"GitLab refused the resend: HTTP {e.code}"
        facts["runs_for_commit_after_resend"] = len(runs_for(sha))
        answers = [json.loads(d["response"]).get("runs") for d in facts.get("deliveries", []) if d["response"].startswith("{")]
        facts["resend_reached_netci_and_matched"] = len(answers) >= 2 and all(a == [done[0]] for a in answers)
        ok = (done[1] == "finished" and done[3] == "SUCCESS" and facts["jenkins_console_line"]
              and sha in facts["jenkins_console_line"] and facts["runs_for_commit_after_resend"] == 1
              and facts["resend_reached_netci_and_matched"])
        facts["verdict"] = "PASS" if ok else "FAIL"
    finally:
        if hook_id and project_id:
            gitlab("DELETE", f"/projects/{project_id}/hooks/{hook_id}")
        gitlab("PUT", "/application/settings", {"outbound_local_requests_whitelist": allow_before})
        forward.terminate()
    restored = gitlab("GET", "/application/settings").get("outbound_local_requests_whitelist") or []
    facts["gitlab_allowlist_restored"] = list(restored) == allow_before
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE / f"webhook-gitlab-{started.strftime('%Y%m%dT%H%M%SZ')}.json"
    path.write_text(json.dumps(facts, indent=2) + "\n")
    print(json.dumps(facts, indent=2))
    return 0 if facts.get("verdict") == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
