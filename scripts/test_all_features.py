#!/usr/bin/env python3
"""Comprehensive test script to exercise every portal endpoint across all 3 systems."""

import json
import random
import urllib.request
import uuid

API = "http://127.0.0.1:8000"

def req(path, method="GET", body=None, headers=None):
    if headers is None:
        headers = {}
    if body is not None and not isinstance(body, (str, bytes)):
        body = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    
    headers["Accept"] = "application/json"
    r = urllib.request.Request(f"{API}{path}", data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r) as resp:
            data = resp.read().decode()
            return resp.status, json.loads(data) if data else {}
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode()
        try:
            parsed = json.loads(err_body)
        except:
            parsed = err_body
        return exc.code, parsed

def run_tests():
    print("=== STARTING COMPREHENSIVE ENDPOINT TESTS ===")
    results = []

    def check(name, status, expected_codes=(200, 201, 202), payload=None):
        ok = status in expected_codes
        results.append((name, ok, status, payload))
        symbol = "✓" if ok else "✗"
        print(f"[{symbol}] {name} -> Status: {status}")
        if not ok:
            print(f"    Payload: {payload}")

    # 1. Dashboard
    st, data = req("/portal/dashboard")
    check("GET /portal/dashboard", st, payload=data)

    # 2. Systems
    st, systems = req("/systems")
    check("GET /systems", st, payload=systems)

    # 3. Create System
    new_sys_id = f"sys-test-{uuid.uuid4().hex[:4]}"
    st, sys_data = req("/systems", "POST", {
        "id": new_sys_id,
        "unit": "Test Unit",
        "description": "Test System Created by Automation",
        "owner": "Tester"
    }, {"Idempotency-Key": str(uuid.uuid4()), "X-Correlation-Id": str(uuid.uuid4())})
    check("POST /systems (Create System)", st, payload=sys_data)

    # 4. Get System Details for all 3 target systems
    for sys_id in ["hello-container", "hello-kubernetes", "hello-systemd-go"]:
        st, sys_detail = req(f"/systems/{sys_id}")
        check(f"GET /systems/{sys_id}", st, payload=sys_detail)

        # System DORA
        st, dora_sys = req(f"/systems/{sys_id}/dora")
        check(f"GET /systems/{sys_id}/dora", st, payload=dora_sys)

    # 5. Create Module via Wizard API
    sub_name = f"submod-{uuid.uuid4().hex[:4]}"
    st, mod_created = req("/systems/hello-container/modules", "POST", {
        "name": sub_name,
        "displayName": f"Submodule {sub_name}",
        "repositoryUrl": f"https://github.com/example/{sub_name}",
        "pipelineTemplate": "container-ci-cd-v1",
        "runtime": "docker",
        "moduleType": "Backend",
        "description": "Submodule created during testing",
        "defaultEnvironment": "dev",
        "deploymentEnvironments": [
            {"displayName": "Development", "environment": "dev", "runtime": "docker", "servers": ["localhost"], "tasks": ["Health check"]}
        ]
    }, {"Idempotency-Key": str(uuid.uuid4()), "X-Correlation-Id": str(uuid.uuid4())})
    check("POST /systems/hello-container/modules (New Module)", st, payload=mod_created)

    # 6. Module Operations for 3 systems
    last_vtags = {}
    for mod_id in ["hello-container", "hello-kubernetes", "hello-systemd-go"]:
        # Get Module
        st, mod_info = req(f"/modules/{mod_id}")
        check(f"GET /modules/{mod_id}", st, payload=mod_info)

        # Overview
        st, overview = req(f"/modules/{mod_id}/overview")
        check(f"GET /modules/{mod_id}/overview", st, payload=overview)

        # Register Version (Dynamic Tag to support repeat runs)
        import time
        vtag = f"v1.4.{int(time.time()) % 10000 + int(uuid.uuid4().int % 100)}"
        last_vtags[mod_id] = vtag
        st, ver_resp = req(f"/modules/{mod_id}/versions", "POST", {
            "tag": vtag,
            "gitTagUrl": f"https://github.com/example/{mod_id}/releases/tag/{vtag}",
            "artifactUrl": f"http://127.0.0.1:55000/{mod_id}:{vtag}",
            "createdBy": "Automation Test"
        }, {"X-Correlation-Id": str(uuid.uuid4())})
        check(f"POST /modules/{mod_id}/versions ({vtag})", st, payload=ver_resp)

        # List Versions
        st, ver_list = req(f"/modules/{mod_id}/versions")
        check(f"GET /modules/{mod_id}/versions", st, payload=ver_list)

        # Start Pipeline
        st, pipe_run = req(f"/modules/{mod_id}/pipeline-runs", "POST", {
            "commitSha": "abc1234",
            "branch": "main",
            "environment": "dev"
        }, {"Idempotency-Key": str(uuid.uuid4()), "X-Correlation-Id": str(uuid.uuid4())})
        check(f"POST /modules/{mod_id}/pipeline-runs", st, payload=pipe_run)

        if st == 202 and "id" in pipe_run:
            run_id = pipe_run["id"]
            # Get Pipeline Run
            st, run_info = req(f"/pipeline-runs/{run_id}")
            check(f"GET /pipeline-runs/{run_id}", st, payload=run_info)

            # Get Logs
            st, logs = req(f"/pipeline-runs/{run_id}/logs")
            check(f"GET /pipeline-runs/{run_id}/logs", st, payload=logs)

        # DORA
        st, dora_mod = req(f"/modules/{mod_id}/dora")
        check(f"GET /modules/{mod_id}/dora", st, payload=dora_mod)

    # 7. Production Requests
    st, pr_list = req("/production-requests")
    check("GET /production-requests", st, payload=pr_list)

    # Create Production Request
    target_v1 = last_vtags.get("hello-container", "v1.0.0")
    st, pr_created = req("/production-requests", "POST", {
        "modules": [{"moduleId": "hello-container", "version": target_v1, "deploymentOrder": 1}],
        "scheduledFor": "2026-08-30T10:00:00Z",
        "rollbackStrategy": "automatic",
        "runAutomationTests": True
    }, {"Idempotency-Key": str(uuid.uuid4()), "X-Correlation-Id": str(uuid.uuid4())})
    check("POST /production-requests (Create PR)", st, payload=pr_created)

    if st == 201 and "id" in pr_created:
        pr_id = pr_created["id"]

        # Reject Request
        st, pr_rej = req(f"/production-requests/{pr_id}/reject", "POST", {
            "comment": "Rejecting for test validation"
        }, {"X-Correlation-Id": str(uuid.uuid4())})
        check(f"POST /production-requests/{pr_id}/reject", st, payload=pr_rej)

    # Create another PR for Approve
    target_v2 = last_vtags.get("hello-kubernetes", "v1.0.0")
    st, pr_created2 = req("/production-requests", "POST", {
        "modules": [{"moduleId": "hello-kubernetes", "version": target_v2, "deploymentOrder": 1}],
        "scheduledFor": "2026-08-31T10:00:00Z",
        "rollbackStrategy": "manual",
        "runAutomationTests": True
    }, {"Idempotency-Key": str(uuid.uuid4()), "X-Correlation-Id": str(uuid.uuid4())})
    check("POST /production-requests (Create PR 2)", st, payload=pr_created2)

    if st == 201 and "id" in pr_created2:
        pr_id2 = pr_created2["id"]
        # Approve Request
        st, pr_app = req(f"/production-requests/{pr_id2}/approve", "POST", {
            "comment": "Approved for deployment"
        }, {"X-Correlation-Id": str(uuid.uuid4())})
        check(f"POST /production-requests/{pr_id2}/approve", st, payload=pr_app)

    # 8. DCIM & Servers
    st, dcim_svc = req("/dcim/services?query=hello")
    check("GET /dcim/services", st, payload=dcim_svc)

    st, dcim_mod = req("/dcim/modules?systemId=hello-container")
    check("GET /dcim/modules", st, payload=dcim_mod)

    st, servers = req("/servers")
    check("GET /servers", st, payload=servers)

    st, audit = req("/audit-events")
    check("GET /audit-events", st, payload=audit)

    # 9. Test DELETE endpoints
    temp_sys = f"temp-sys-{random.randint(1000, 9999)}"
    st, sys_created = req("/systems", "POST", {
        "id": temp_sys, "unit": "Temp Unit", "description": "Temp system for delete test", "owner": "Admin"
    }, {"Idempotency-Key": str(uuid.uuid4()), "X-Correlation-Id": str(uuid.uuid4())})
    check(f"POST /systems (Create {temp_sys})", st, payload=sys_created)

    temp_mod = f"temp-mod-{random.randint(1000, 9999)}"
    st, mod_created = req(f"/systems/{temp_sys}/modules", "POST", {
        "name": temp_mod,
        "displayName": "Temp Module",
        "repositoryUrl": "https://github.com/example/temp-mod.git",
        "pipelineTemplate": "container-ci-cd-v1",
        "runtime": "docker",
        "moduleType": "Backend",
        "description": "Temp module for delete test",
        "defaultEnvironment": "dev",
        "deploymentEnvironments": [{
            "displayName": "Development",
            "environment": "dev",
            "runtime": "docker",
            "servers": ["srv-dev-01.local"],
            "tasks": ["docker-compose up -d"]
        }]
    }, {"Idempotency-Key": str(uuid.uuid4()), "X-Correlation-Id": str(uuid.uuid4())})
    check(f"POST /systems/{temp_sys}/modules (Create {temp_mod})", st, payload=mod_created)

    st, del_mod = req(f"/modules/{temp_mod}", "DELETE")
    check(f"DELETE /modules/{temp_mod}", st, expected_codes=(204,))

    st, del_sys = req(f"/systems/{temp_sys}", "DELETE")
    check(f"DELETE /systems/{temp_sys}", st, expected_codes=(204,))

    passed = sum(1 for r in results if r[1])
    failed = sum(1 for r in results if not r[1])
    print(f"\n=== SUMMARY: {passed} PASSED, {failed} FAILED ===")
    return failed == 0

if __name__ == "__main__":
    success = run_tests()
    exit(0 if success else 1)
