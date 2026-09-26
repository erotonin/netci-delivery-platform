#!/usr/bin/env bash
# Projects and netCI's robot account in the lab Harbor. Idempotent. Nothing secret is printed:
# the robot's secret goes straight to .netci-gate/corp/harbor_robot_secret (mode 600).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
SECRETS="${ROOT}/.netci-gate/corp"
URL="${HARBOR_URL:-http://172.17.0.1:8930}"
exec 3<"${SECRETS}/harbor_admin_password"
python3 - "${URL}" "${SECRETS}" <<'PY'
import base64, json, os, sys, urllib.error, urllib.request
url, secrets = sys.argv[1:]
password = os.fdopen(3).read().strip()
auth = "Basic " + base64.b64encode(f"admin:{password}".encode()).decode()

def call(method, path, body=None):
    request = urllib.request.Request(url + "/api/v2.0" + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": auth, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as error:
        return error.code, None

# mirror and netci are pulled by every node and agent pod; apps holds what builds publish.
for name, public in (("netci", True), ("mirror", True), ("apps", True)):
    status, _ = call("POST", "/projects", {"project_name": name, "metadata": {"public": str(public).lower()}})
    print(f"project {name}: {'created' if status == 201 else 'exists' if status == 409 else status}")

status, robots = call("GET", "/robots?q=name%3Dnetci")
if robots:
    print("robot robot$netci: exists (secret not re-issued)")
else:
    access = [{"resource": "repository", "action": a} for a in ("push", "pull")]
    status, robot = call("POST", "/robots", {
        "name": "netci", "description": "netCI builds: push and pull", "duration": -1, "level": "system",
        "disable": False,
        "permissions": [{"kind": "project", "namespace": p, "access": access} for p in ("netci", "mirror", "apps")],
    })
    if status != 201:
        sys.exit(f"robot creation failed: HTTP {status}")
    old = os.umask(0o077)
    open(f"{secrets}/harbor_robot_name", "w").write(robot["name"])
    open(f"{secrets}/harbor_robot_secret", "w").write(robot["secret"])
    os.umask(old)
    print(f"robot {robot['name']}: created, secret in .netci-gate/corp/harbor_robot_secret")
PY
