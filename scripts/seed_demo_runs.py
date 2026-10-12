#!/usr/bin/env python3
"""Seed real pipeline runs, deployments, and production requests into live local backend API."""

import json
import urllib.request
import uuid
from datetime import datetime, timezone
import os

API = os.getenv("NETCI_API_URL", "http://127.0.0.1:8100")

def get_json(url: str):
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())

def post_json(url: str, data: dict, idempotency_key: str = None, bearer_token: str = None):
    payload = json.dumps(data).encode()
    headers = {
        "Content-Type": "application/json",
    }
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
        headers["X-Correlation-Id"] = idempotency_key
    else:
        headers["Idempotency-Key"] = str(uuid.uuid4())
        headers["X-Correlation-Id"] = str(uuid.uuid4())

    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())
    except Exception as exc:
        print(f"POST {url} failed: {exc}")
        return None

def main():
    print("Fetching portal dashboard...")
    dashboard = get_json(f"{API}/portal/dashboard")
    systems = dashboard.get("systems", [])
    
    for sys in systems:
        sys_id = sys["id"]
        modules = sys.get("modules", [])
        for mod in modules:
            app_id = mod.get("applicationId")
            if not app_id:
                continue
            
            print(f"Seeding pipeline runs & deployments for {sys_id} ({app_id})...")
            
            # Run 1: Successful Dev Run
            commit_sha = f"c{uuid.uuid4().hex[:7]}"
            run = post_json(
                f"{API}/applications/{app_id}/pipeline-runs",
                {
                    "commitSha": commit_sha,
                    "branch": "main",
                    "environment": "dev",
                    "parameters": {"commitTimestamp": datetime.now(timezone.utc).isoformat()},
                }
            )
            if run and "id" in run:
                run_id = run["id"]
                # CI running
                post_json(
                    f"{API}/pipeline-runs/{run_id}/ci-result",
                    {"status": "running", "logLines": ["Starting build step...", "Compiling binaries..."]},
                    bearer_token="netci-local-pipeline-key"
                )
                # CI succeeded
                digest = "sha256:" + "a" * 64
                post_json(
                    f"{API}/pipeline-runs/{run_id}/ci-result",
                    {"status": "succeeded", "artifactDigest": digest, "logLines": ["Container image pushed to registry.", "Tests passed: 42/42."]},
                    bearer_token="netci-local-pipeline-key"
                )
                
                # Deploy to dev
                deploy = post_json(
                    f"{API}/applications/{app_id}/deployments",
                    {"pipelineRunId": run_id, "environment": "dev", "version": "v1.2.0"}
                )
                if deploy and "id" in deploy:
                    deploy_id = deploy["id"]
                    post_json(
                        f"{API}/deployments/{deploy_id}/result",
                        {"status": "healthy", "detail": "Service deployed and healthy on localhost"},
                        bearer_token="netci-local-pipeline-key"
                    )

    # Seed initial Production Request
    print("Creating production request...")
    post_json(
        f"{API}/production-requests",
        {
            "modules": [{"moduleId": "hello-container", "version": "v1.2.0", "deploymentOrder": 1}],
            "scheduledFor": datetime.now(timezone.utc).isoformat(),
            "rollbackStrategy": "Automatic rollback on healthcheck failure",
            "runAutomationTests": True
        }
    )
    print("Seeding complete!")

if __name__ == "__main__":
    main()
