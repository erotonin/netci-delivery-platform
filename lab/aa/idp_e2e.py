#!/usr/bin/env python3
"""The IDP end to end on the active-active replicas (ADR-091), through its public API as people use it.

    set -a; source .netci-gate/aa/idp.env; set +a
    python3 lab/aa/idp_e2e.py onboard        system ha-payments, module payments-api (runtime kubernetes)
    python3 lab/aa/idp_e2e.py build [--deploy]  build GitLab main's head (dev deployment with --deploy)
    python3 lab/aa/idp_e2e.py status RUN_ID

Sign-in is Keycloak's (pat: admin, dana: developer, rae: reviewer); every step's answer is kept in
lab/evidence/aa-idp-e2e-<timestamp>.json. Tokens and passwords are never printed.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
API = os.environ.get("NETCI_API_URL", "http://127.0.0.1:18100").rstrip("/")
SYSTEM, MODULE = "ha-payments", "payments-api"
REPO = "http://172.17.0.1:8929/platform/payments-api.git"


def token(user_var: str) -> str:
    raw = os.environ[user_var]
    user, password = raw.split(":", 1) if ":" in raw else (raw, os.environ["NETCI_LAB_USER_PASSWORD"])
    form = urllib.parse.urlencode({"grant_type": "password", "client_id": os.environ["NETCI_ACCEPTANCE_OIDC_CLIENT_ID"],
                                   "client_secret": os.environ.get("NETCI_ACCEPTANCE_OIDC_CLIENT_SECRET", ""),
                                   "username": user, "password": password, "scope": "openid"}).encode()
    with urllib.request.urlopen(urllib.request.Request(os.environ["NETCI_ACCEPTANCE_OIDC_TOKEN_URL"], data=form), timeout=20) as r:
        return json.load(r)["id_token"]


def call(method: str, path: str, tok: str, body=None, headers=None) -> tuple[int, object]:
    req = urllib.request.Request(API + path, method=method, data=None if body is None else json.dumps(body).encode())
    req.add_header("Authorization", f"Bearer {tok}")
    req.add_header("Accept", "application/json")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw, code = r.read(), r.status
    except urllib.error.HTTPError as e:
        raw, code = e.read(), e.code
    try:
        return code, json.loads(raw) if raw else None
    except ValueError:
        return code, raw.decode(errors="replace")


EVIDENCE: dict = {"scenario": "aa-idp-e2e", "started": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "steps": []}


def step(name: str, code: int, body: object, ok: bool) -> None:
    EVIDENCE["steps"].append({"step": name, "code": code, "ok": ok, "body": body})
    print(f"[{'ok' if ok else 'FAIL'}] {name}: {code}", flush=True)


def save() -> None:
    out = ROOT / "lab" / "evidence" / f"aa-idp-e2e-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    out.write_text(json.dumps(EVIDENCE, indent=1, default=str) + "\n")
    print(f"evidence {out.relative_to(ROOT)}")


def target(env: str) -> dict:
    return {"displayName": {"dev": "Development", "staging": "Staging", "prod": "Production"}[env], "environment": env,
            "runtime": "kubernetes", "servers": [], "tasks": [], "kubeconfigRef": "aa-lab", "namespace": env,
            "runtimeSettings": {"imagePullHost": "172.17.0.1:8930"}}


def onboard() -> None:
    admin = token("NETCI_ACCEPTANCE_USER_ADMIN")
    code, body = call("POST", "/systems", admin, {"id": SYSTEM, "unit": "Payments", "description": "Payments, on the active-active Jenkins"})
    step("create system", code, body, code in (201, 409))
    code, body = call("POST", f"/systems/{SYSTEM}/modules", admin, {
        "name": MODULE, "repositoryUrl": REPO, "pipelineTemplate": "kubernetes-ci-cd-v1", "runtime": "kubernetes",
        "defaultEnvironment": "dev", "deploymentEnvironments": [target(e) for e in ("dev", "staging", "prod")],
    }, {"Idempotency-Key": "ha-payments-api-create-v1"})
    step("create module", code, body, code in (201, 409))
    code, body = call("GET", f"/modules/{MODULE}", admin)
    step("read module", code, {k: body.get(k) for k in ("id", "applicationId", "runtime", "repositoryUrl")} if code == 200 else body, code == 200)


def build(deploy: bool) -> None:
    dev = token("NETCI_ACCEPTANCE_USER_DEVELOPER")
    code, refs = call("GET", f"/modules/{MODULE}/git-refs", dev)
    head = next((b["sha"] for b in (refs or {}).get("branches", []) if b["name"] == "main"), None) if code == 200 else None
    step("read git refs", code, {"main": head, "error": (refs or {}).get("error") if isinstance(refs, dict) else refs}, bool(head))
    if not head:
        return
    code, run = call("POST", f"/modules/{MODULE}/pipeline-runs", dev, {"commitSha": head, "branch": "main", "environment": "dev", "deploy": deploy},
                     {"Idempotency-Key": f"ha-e2e-{head[:12]}-{'deploy' if deploy else 'build'}-{int(time.time())}"})
    step("start run", code, run, code == 202)
    if code != 202:
        return
    follow(dev, run["id"])


def follow(tok: str, run_id: str, timeout: float = 1800) -> None:
    end, last = time.time() + timeout, None
    while time.time() < end:
        code, run = call("GET", f"/pipeline-runs/{run_id}", tok)
        state = run.get("status") if isinstance(run, dict) else None
        if state != last:
            print(f"  run {run_id[:8]}: {state} {run.get('jenkinsRunId') or ''}", flush=True)
            last = state
        if state in ("succeeded", "failed", "cancelled"):
            break
        time.sleep(10)
    code, stages = call("GET", f"/pipeline-runs/{run_id}/stages", tok)
    items = stages.get("items", []) if isinstance(stages, dict) else []
    step("run finished", code, {"run": run, "stages": [{k: s.get(k) for k in ("stageId", "status", "durationMs", "errorMessage")} for s in items]},
         state == "succeeded")
    for s in items:
        print(f"    {s.get('stageId'):20s} {s.get('status'):10s} {s.get('durationMs') or ''} {s.get('errorMessage') or ''}")


def main() -> int:
    what = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        if what == "onboard":
            onboard()
        elif what == "build":
            build("--deploy" in sys.argv)
        elif what == "status":
            follow(token("NETCI_ACCEPTANCE_USER_DEVELOPER"), sys.argv[2], timeout=5)
        else:
            print(__doc__)
            return 2
    finally:
        if EVIDENCE["steps"]:
            save()
    return 0 if all(s["ok"] for s in EVIDENCE["steps"]) else 1


if __name__ == "__main__":
    sys.exit(main())
