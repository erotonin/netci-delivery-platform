#!/usr/bin/env python3
"""End-to-end proof on the corp lab: payments-api built from GitLab by the one Jenkins
controller, published to Harbor, with evidence netCI accepted. Build-only (ADR-043): the lab
deploys it to the lab's own target host netci-corp-app-01 (scripts/corp/app_host.sh) over SSH.

    set -a; source .netci-gate/real-local.env; set +a
    NETCI_API_URL=http://127.0.0.1:18100 .venv/bin/python scripts/corp/e2e_build.py

Drafted by Gemini from the acceptance harness and the API models; reviewed and corrected.
"""
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

def fail(msg: str):
    print(f"[FAIL] {msg}")
    sys.exit(1)

def ok(msg: str):
    print(f"[ok] {msg}")

class Api:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def call(self, method: str, path: str, token: str = None, body=None, headers=None, timeout=30.0):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"{self.base_url}{path}", data=data, method=method)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                raw = response.read()
                status = response.status
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            status = exc.code
        try:
            return status, json.loads(raw) if raw else None
        except ValueError:
            return status, raw.decode(errors="replace")

def _password_grant(username, password):
    token_url = os.environ["NETCI_ACCEPTANCE_OIDC_TOKEN_URL"]
    client_id = os.environ["NETCI_ACCEPTANCE_OIDC_CLIENT_ID"]
    client_secret = os.environ.get("NETCI_ACCEPTANCE_OIDC_CLIENT_SECRET", "")
    form = urllib.parse.urlencode({
        "grant_type": "password",
        "client_id": client_id,
        "client_secret": client_secret,
        "username": username,
        "password": password,
        "scope": "openid",
    }).encode()
    req = urllib.request.Request(token_url, data=form, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            payload = json.loads(response.read())
            return str(payload["id_token"])
    except urllib.error.HTTPError as exc:
        fail(f"Token acquisition failed for {username}: {exc.code} {exc.read().decode('utf-8', errors='replace')}")
    except Exception as e:
        fail(f"Token acquisition failed for {username}: {e}")

def wait_until(probe, timeout=1800.0, interval=10.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = probe()
        if last:
            return last
        time.sleep(interval)
    fail(f"Timed out after {timeout}s")

def main():
    api_url = os.environ.get("NETCI_API_URL", "http://127.0.0.1:18100")
    api = Api(api_url)

    def get_credentials(var_name):
        val = os.environ[var_name]
        if ":" in val:
            return val.split(":", 1)
        return val, os.environ["NETCI_LAB_USER_PASSWORD"]
    
    admin_user, admin_pass = get_credentials("NETCI_ACCEPTANCE_USER_ADMIN")
    dev_user, dev_pass = get_credentials("NETCI_ACCEPTANCE_USER_DEVELOPER")
    
    admin_token = _password_grant(admin_user, admin_pass)
    dev_token = _password_grant(dev_user, dev_pass)
    ok("Tokens for admin and developer")

    sys_body = {"id": "corp-payments", "unit": "corp", "description": "Corporate Payments"}
    status, body = api.call("POST", "/systems", token=admin_token, body=sys_body)
    if status not in (201, 409):
        fail(f"Create system failed: {status} {body}")
    ok("Created system corp-payments")

    # A real target: netci-corp-app-01, in the worker's inventory (values-lab-corp.yaml).
    mod_body = {
        "name": "payments-api",
        "repositoryUrl": "http://172.17.0.1:8929/platform/payments-api.git",
        "pipelineTemplate": "container-ci-cd-v1",
        "runtime": "docker",
        "defaultEnvironment": "dev",
        "deploymentEnvironments": [
            {
                "displayName": "Development",
                "environment": "dev",
                "runtime": "docker",
                "servers": ["netci-corp-app-01"]
            }
        ]
    }
    idempotency_key = "corp-payments-api-create-v1"  # stable: a re-run replays, not duplicates
    status, body = api.call("POST", "/systems/corp-payments/modules", token=admin_token, body=mod_body, headers={"Idempotency-Key": idempotency_key})
    if status not in (201, 409):
        fail(f"Create module failed: {status} {body}")
    
    status, module = api.call("GET", "/modules/payments-api", token=admin_token)
    if status != 200:
        fail(f"Failed to GET module: {status} {module}")
    app_id = module["applicationId"]
    ok("Created module payments-api")

    secret_path = Path(".netci-gate/corp/payments_api_webhook_secret")
    if secret_path.exists():
        webhook_secret = secret_path.read_text().strip()
    else:
        webhook_secret = secrets.token_urlsafe(32)
        # Created 0600, not written then chmod-ed: no moment where it is world-readable.
        fd = os.open(secret_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(webhook_secret)

    scm_body = {
        "provider": "gitlab",
        "repositoryIdentity": "platform/payments-api",
        "secretToken": webhook_secret
    }
    status, body = api.call("POST", f"/applications/{app_id}/scm", token=admin_token, body=scm_body)
    if status not in (201, 409):
        fail(f"SCM integration failed: {status} {body}")
    ok("Registered GitLab SCM integration")

    # GitLab -> netCI: the project's webhook, signed with the same secret (idempotent by URL).
    hook_url = "http://netci-corp-worker:30800/webhooks/scm/gitlab"
    admin_gitlab = Path(".netci-gate/corp/gitlab_admin_token").read_text().strip()
    def gitlab(method, path, body=None):
        request = urllib.request.Request("http://172.17.0.1:8929/api/v4" + path, method=method,
                                         data=json.dumps(body).encode() if body is not None else None,
                                         headers={"PRIVATE-TOKEN": admin_gitlab, "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read() or b"null")
    hooks = gitlab("GET", "/projects/platform%2Fpayments-api/hooks")
    hook = {"url": hook_url, "token": webhook_secret, "push_events": True, "merge_requests_events": True,
            "enable_ssl_verification": False}
    existing = [h for h in hooks if h["url"] == hook_url]
    gitlab("PUT" if existing else "POST",
           f"/projects/platform%2Fpayments-api/hooks/{existing[0]['id']}" if existing else "/projects/platform%2Fpayments-api/hooks",
           hook)
    ok(f"GitLab webhook -> {hook_url}")

    gitlab_token_path = Path(".netci-gate/corp/gitlab_netci_token")
    gitlab_token = gitlab_token_path.read_text().strip()
    gitlab_req = urllib.request.Request(
        "http://172.17.0.1:8929/api/v4/projects/platform%2Fpayments-api/repository/branches/main",
        headers={"PRIVATE-TOKEN": gitlab_token}
    )
    try:
        with urllib.request.urlopen(gitlab_req) as response:
            branch_info = json.loads(response.read())
            commit_sha = branch_info["commit"]["id"]
    except urllib.error.HTTPError as exc:
        fail(f"GitLab commit resolution failed: {exc.code} {exc.read().decode('utf-8', errors='replace')}")
    ok(f"Resolved commit {commit_sha}")

    run_body = {
        "commitSha": commit_sha,
        "branch": "main",
        "environment": "dev",
    }
    run_key = f"e2e-run-{int(time.time())}"
    status, run_info = api.call("POST", "/modules/payments-api/pipeline-runs", token=dev_token, body=run_body, headers={"Idempotency-Key": run_key})
    if status != 202:
        fail(f"Start pipeline failed: {status} {run_info}")
    run_id = run_info["id"]
    ok("Started pipeline run")

    last_status = None
    def check_run():
        nonlocal last_status
        st, current = api.call("GET", f"/pipeline-runs/{run_id}", token=dev_token)
        if st != 200:
            return None
        run_status = current.get("status")
        if run_status != last_status:
            print(f"Run status changed: {run_status}")
            last_status = run_status
        if run_status in ("succeeded", "failed", "cancelled", "rolled_back"):
            return current
        return None

    final_run = wait_until(check_run, timeout=1800, interval=10.0)
    if final_run["status"] != "succeeded":
        fail(f"Run failed with reason: {final_run.get('errorMessage', 'unknown')}")
    ok("Run succeeded")

    status, evidence = api.call("GET", f"/pipeline-runs/{run_id}/security-evidence", token=admin_token)
    if status != 200:
        fail(f"Failed to fetch security evidence: {status} {evidence}")
    
    digest = evidence.get("artifactDigest", "")
    if not digest.startswith("sha256:"):
        fail(f"Invalid artifactDigest: {digest}")
    
    signature = evidence.get("signature", {})
    if not signature.get("verified"):
        fail("Signature not verified")
        
    tool_versions = evidence.get("toolVersions", {})
    for tool in ["syft", "trivy", "cosign"]:
        if tool not in tool_versions:
            fail(f"Tool {tool} missing in toolVersions")
            
    if evidence.get("decision") != "allow":
        fail(f"Policy decision not allowed: {evidence.get('decision')}")
        
    checks = evidence.get("checks", {})
    ok(f"Security evidence verified, decision checks: {checks}")

    status, toolchain = api.call("GET", "/toolchain", token=admin_token)
    if status != 200:
        fail(f"Failed to get toolchain: {status} {toolchain}")
    
    # GET /toolchain: {declared, observed: [{controllerId, toolVersions, ...}], drift: [...]}
    # The controller is reported as "jenkins-<id>" (id "corp" in values-lab-corp.yaml).
    observed = [o for o in toolchain.get("observed", []) if str(o.get("controllerId", "")).lower() in {"corp", "jenkins-corp"}]
    if not observed:
        fail("GET /toolchain has no observed tool versions from controller corp")
    drift = [d for d in toolchain.get("drift", []) if str(d.get("controllerId", "jenkins-corp")).lower() in {"corp", "jenkins-corp"}]
    ok(f"Toolchain corp observed {observed[0].get('toolVersions')}; drift {drift or 'none'}; "
       f"trivyDb {toolchain.get('trivyDb')}")

    artifact_ref = evidence.get("artifactRef") or evidence.get("artifactDigest")
    ok(f"Artifact reference: {artifact_ref}")

    # The deployment the run started: real worker, real SSH to netci-corp-app-01.
    def deployment():
        st, body = api.call("GET", f"/deployments?applicationId={app_id}", token=admin_token)
        items = body if isinstance(body, list) else (body or {}).get("items", [])
        return next((d for d in items if d.get("pipelineRunId") == run_id), None)

    last_deploy = None
    def deployed():
        nonlocal last_deploy
        current = deployment()
        if current and current.get("status") != last_deploy:
            last_deploy = current.get("status")
            print(f"Deployment status: {last_deploy}")
        terminal = {"healthy", "failed", "rolled_back", "rollback_failed", "cancelled"}
        return current if current and current.get("status") in terminal else None

    final = wait_until(deployed, timeout=900, interval=10.0)
    if final["status"] != "healthy":
        fail(f"Deployment ended {final['status']}: {final.get('failureReason') or final.get('reason') or ''}")
    ok(f"Deployment {final['id']} healthy on {final.get('targetHosts') or final.get('servers')}")

if __name__ == "__main__":
    main()
