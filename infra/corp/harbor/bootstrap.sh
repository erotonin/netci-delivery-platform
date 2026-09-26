#!/usr/bin/env bash
# Projects, robot accounts and the Trivy DB replication in the lab Harbor. Idempotent. Nothing secret is printed:
# the robot's secret goes straight to .netci-gate/corp/harbor_robot_secret (mode 600).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
SECRETS="${ROOT}/.netci-gate/corp"
URL="${HARBOR_URL:-https://172.17.0.1:8930}"  # TLS from the lab CA
exec 3<"${SECRETS}/harbor_admin_password"
python3 - "${URL}" "${SECRETS}" <<'PY'
import base64, json, os, ssl, sys, urllib.error, urllib.request
url, secrets = sys.argv[1:]
# Harbor's certificate chains to the lab CA; verified, never skipped.
tls = ssl.create_default_context(cafile=f"{secrets}/pki/ca.crt")
password = os.fdopen(3).read().strip()
auth = "Basic " + base64.b64encode(f"admin:{password}".encode()).decode()

def call(method, path, body=None):
    request = urllib.request.Request(url + "/api/v2.0" + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": auth, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=120, context=tls) as response:  # registry create pings upstream
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as error:
        return error.code, None

# mirror and netci are pulled by every node and agent pod; apps holds what builds publish.
for name, public in (("netci", True), ("mirror", True), ("apps", True)):
    status, _ = call("POST", "/projects", {"project_name": name, "metadata": {"public": str(public).lower()}})
    print(f"project {name}: {'created' if status == 201 else 'exists' if status == 409 else status}")

def pull(p): return [{"resource": "repository", "action": "pull"}]
def push_pull(p): return [{"resource": "repository", "action": a} for a in ("push", "pull")]

def ensure_robot(name, description, grants, name_file, secret_file):
    """A system robot with exactly `grants` ({project: access-fn}). An existing one is
    brought to those permissions, its secret left alone."""
    permissions = [{"kind": "project", "namespace": p, "access": fn(p)} for p, fn in grants.items()]
    status, robots = call("GET", f"/robots?q=name%3D{name}")
    if robots:
        robot = robots[0]
        body = {k: robot[k] for k in ("name", "description", "duration", "level", "disable", "editable") if k in robot}
        body["permissions"] = permissions
        status, _ = call("PUT", f"/robots/{robot['id']}", body)
        print(f"robot {robot['name']}: permissions set ({'ok' if status == 200 else status})")
        if status != 200:
            sys.exit(f"robot {name} update failed: HTTP {status}")
        return
    status, robot = call("POST", "/robots", {"name": name, "description": description, "duration": -1,
                                             "level": "system", "disable": False, "permissions": permissions})
    if status != 201:
        sys.exit(f"robot {name} creation failed: HTTP {status}")
    old = os.umask(0o077)
    open(f"{secrets}/{name_file}", "w").write(robot["name"])
    open(f"{secrets}/{secret_file}", "w").write(robot["secret"])
    os.umask(old)
    print(f"robot {robot['name']}: created, secret in .netci-gate/corp/{secret_file}")

# Builds run code nobody has reviewed yet (a pull request's), so their robot pushes only to
# `apps`: it must not be able to overwrite a mirrored base image, the Trivy DB or netCI's
# own images, which every later build and node trusts.
ensure_robot("netci", "netCI builds: pull netci/mirror, push apps",
             {"netci": pull, "mirror": pull, "apps": push_pull}, "harbor_robot_name", "harbor_robot_secret")
# Mirroring and publishing netCI's images is an operator's job (scripts/corp/up.sh).
ensure_robot("ops", "operators: publish netci and mirror images",
             {"netci": push_pull, "mirror": push_pull}, "harbor_ops_robot_name", "harbor_ops_robot_secret")

# A published tag keeps meaning the same bytes: netCI's own images and every mirrored upstream
# are immutable in Harbor -- except the Trivy DB, whose one tag is refreshed every 6 hours.
# `apps` stays mutable: netCI identifies artifacts by digest, and a re-run of the same commit
# may push its tag again.
def ensure_immutable(project, exclude=None):
    status, rules = call("GET", f"/projects/{project}/immutabletagrules")
    if rules:
        print(f"immutability {project}: rule exists")
        return
    repo = {"kind": "doublestar", "decoration": "repoExcludes" if exclude else "repoMatches",
            "pattern": exclude or "**"}
    status, _ = call("POST", f"/projects/{project}/immutabletagrules", {
        "disabled": False, "action": "immutable", "template": "immutable_template",
        "tag_selectors": [{"kind": "doublestar", "decoration": "matches", "pattern": "**"}],
        "scope_selectors": {"repository": [repo]},
    })
    print(f"immutability {project}: {'created' if status == 201 else status}")
    if status != 201:
        sys.exit(f"immutability rule for {project} failed")

ensure_immutable("netci")
ensure_immutable("mirror", exclude="aquasec/trivy-db")

# The Trivy DB is pulled from its source on a schedule, into the mirror everything scans
# with: a stale DB makes the toolchain gate refuse builds (ADR-056), so freshness is
# Harbor's job, not someone's memory. Anonymous pull from ghcr.io.
# Needs egress from Harbor's containers. This lab host runs Docker with iptables and IP
# forwarding off on purpose, so they have none; scripts/corp/mirror_trivy_db.sh does the same
# from the host on a timer instead. Said, not failed: the rest of the bootstrap is still valid.
status, registries = call("GET", "/registries?q=name%3Dghcr")
if registries:
    registry_id = registries[0]["id"]
else:
    status, _ = call("POST", "/registries", {"name": "ghcr", "type": "github-ghcr", "url": "https://ghcr.io", "insecure": False})
    if status != 201:
        print(f"replication trivy-db: skipped -- Harbor cannot reach ghcr.io (HTTP {status}); "
              "use scripts/corp/mirror_trivy_db.sh")
        sys.exit(0)
    registry_id = call("GET", "/registries?q=name%3Dghcr")[1][0]["id"]
policy = {
    "name": "trivy-db", "description": "Trivy vulnerability DB from ghcr.io every 6 hours",
    "src_registry": {"id": registry_id}, "dest_namespace": "mirror/aquasec", "dest_namespace_replace_count": 1,
    "filters": [{"type": "name", "value": "aquasecurity/trivy-db"}, {"type": "tag", "value": "2"}],
    "trigger": {"type": "scheduled", "trigger_settings": {"cron": "0 17 */6 * * *"}},
    "override": True, "enabled": True, "speed": -1,
}
status, policies = call("GET", "/replication/policies?name=trivy-db")
if policies:
    status, _ = call("PUT", f"/replication/policies/{policies[0]['id']}", policy)
    policy_id = policies[0]["id"]
else:
    status, _ = call("POST", "/replication/policies", policy)
    policy_id = call("GET", "/replication/policies?name=trivy-db")[1][0]["id"]
print(f"replication trivy-db: {'ok' if status in (200, 201) else status} (policy {policy_id})")
if status not in (200, 201):
    sys.exit("replication policy failed")
status, _ = call("POST", "/replication/executions", {"policy_id": policy_id})
print(f"replication trivy-db: run now ({'started' if status == 201 else status})")
PY
