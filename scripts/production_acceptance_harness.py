#!/usr/bin/env python3
"""Production acceptance harness: nine gates, each exercised against a running stack.

Every gate performs the operation it is named after, through the public API of a live
netCI, and records what came back. A gate passes only when the real thing happened
(a build ran in Jenkins and produced a digest; cosign verified that digest; a rollback
restored the previous digest and the platform recorded it). A gate that cannot run
because a prerequisite is missing is BLOCKED and says which one; it never passes on the
strength of configuration being present.

Prerequisites, all via environment:

  NETCI_ACCEPTANCE_API_URL          the running API, e.g. http://127.0.0.1:8100
  NETCI_ACCEPTANCE_MODULE           a module whose dev target is deployable (hello-container)
  NETCI_ACCEPTANCE_COMMIT           commit to build (a full SHA the module's repo has)
  NETCI_ACCEPTANCE_OIDC_TOKEN_URL   Keycloak token endpoint for the password grant
  NETCI_ACCEPTANCE_OIDC_CLIENT_ID / _CLIENT_SECRET
  NETCI_ACCEPTANCE_USER_ADMIN / _REVIEWER / _DEVELOPER   "username:password" (lab accounts)
  NETCI_ACCEPTANCE_RETIRED_SERVER   a DCIM device in a blocking state (netci-retired-01)
  NETCI_ACCEPTANCE_WORKER_RESTART   optional command that stops and restarts the Temporal
                                    worker; without it the restart/resume gate is BLOCKED
  NETCI_COSIGN_EXECUTABLE / NETCI_COSIGN_PUBLIC_KEY_FILE   for the independent verify
  DATABASE_URL                      the live database, for the persistence and backup gates

The password grant is a lab convenience: it exercises the real Keycloak realm, role and
group mapping without a browser. A deployment with a proper IdP hands the harness three
bearer tokens instead (NETCI_ACCEPTANCE_TOKEN_ADMIN / _REVIEWER / _DEVELOPER).

Writes evidence/production_acceptance_<stamp>.json and evidence/acceptance.xml, each
stamped with the commit under test.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(ROOT / "backend"))


@dataclass
class GateResult:
    gate: str
    status: str  # PASS, FAIL, BLOCKED
    reason: str
    duration_seconds: float
    details: dict[str, object] = field(default_factory=dict)


class Blocked(Exception):
    """A prerequisite is missing. Not a failure: the gate could not be exercised."""


class Api:
    """Thin client over urllib; every call returns (status, json-or-text)."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def call(self, method: str, path: str, token: str | None = None, body: Any = None,
             headers: dict[str, str] | None = None, timeout: float = 30.0) -> tuple[int, Any]:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(f"{self.base_url}{path}", data=data, method=method)
        request.add_header("Accept", "application/json")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                status = response.status
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            status = exc.code
        try:
            return status, json.loads(raw) if raw else None
        except ValueError:
            return status, raw.decode(errors="replace")


# ------------------------------------------------------------------ identity


def _password_grant(username: str, password: str) -> str:
    token_url = os.environ["NETCI_ACCEPTANCE_OIDC_TOKEN_URL"]
    form = urllib.parse.urlencode({
        "grant_type": "password",
        "client_id": os.environ["NETCI_ACCEPTANCE_OIDC_CLIENT_ID"],
        "client_secret": os.environ.get("NETCI_ACCEPTANCE_OIDC_CLIENT_SECRET", ""),
        "username": username,
        "password": password,
        "scope": "openid",
    }).encode()
    with urllib.request.urlopen(urllib.request.Request(token_url, data=form, method="POST"), timeout=20) as response:
        payload = json.loads(response.read())
    return str(payload["id_token"])


def tokens() -> dict[str, str]:
    """Three real identities: admin, reviewer, developer."""

    out: dict[str, str] = {}
    for role in ("ADMIN", "REVIEWER", "DEVELOPER"):
        direct = os.getenv(f"NETCI_ACCEPTANCE_TOKEN_{role}", "").strip()
        if direct:
            out[role.lower()] = direct
            continue
        credentials = os.getenv(f"NETCI_ACCEPTANCE_USER_{role}", "").strip()
        if not credentials or ":" not in credentials or not os.getenv("NETCI_ACCEPTANCE_OIDC_TOKEN_URL"):
            raise Blocked(f"no identity for the {role.lower()} role (NETCI_ACCEPTANCE_TOKEN_{role} or NETCI_ACCEPTANCE_USER_{role} + OIDC settings)")
        username, password = credentials.split(":", 1)
        out[role.lower()] = _password_grant(username, password)
    return out


# ------------------------------------------------------------------ helpers


def _require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise Blocked(f"{name} is not set")
    return value


def git_commit_sha() -> str:
    for candidate in (os.getenv("NETCI_BUILD_COMMIT", "").strip(), os.getenv("GIT_COMMIT_SHA", "").strip()):
        if candidate:
            return candidate
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT).decode().strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def wait_until(describe: str, probe, *, timeout: float, interval: float = 5.0):
    """Poll `probe()` until it returns a truthy value; raise on timeout."""

    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = probe()
        if last:
            return last
        time.sleep(interval)
    raise TimeoutError(f"timed out after {timeout:.0f}s waiting for {describe} (last: {last!r})")


def gate(name: str):
    """Run a gate function; translate its outcome into a GateResult."""

    def decorate(fn):
        def run(context: dict[str, Any]) -> GateResult:
            start = time.monotonic()
            try:
                reason, details = fn(context)
                return GateResult(name, "PASS", reason, time.monotonic() - start, details)
            except Blocked as exc:
                return GateResult(name, "BLOCKED", str(exc), time.monotonic() - start)
            except Exception as exc:  # noqa: BLE001 - the failure is the finding
                return GateResult(name, "FAIL", f"{type(exc).__name__}: {exc}", time.monotonic() - start)
        run.gate_name = name
        return run
    return decorate


def deployment_for_run(api: Api, token: str, application_id: str, run_id: str) -> dict[str, Any] | None:
    status, body = api.call("GET", f"/deployments?applicationId={application_id}", token)
    items = body if isinstance(body, list) else (body or {}).get("items", [])
    for item in items:
        if item.get("pipelineRunId") == run_id:
            return item
    return None


def start_module_run(api: Api, token: str, module: str, environment: str, commit: str, key: str) -> dict[str, Any]:
    status, run = api.call("POST", f"/modules/{module}/pipeline-runs", token,
                           {"commitSha": commit, "branch": "main", "environment": environment},
                           headers={"Idempotency-Key": key})
    if status != 202:
        raise RuntimeError(f"could not start a {environment} run: {status} {run}")
    return run


def release(api: Api, context: dict[str, Any], environment: str, label: str, *, approve_with: str | None = None) -> dict[str, Any]:
    """Build, (approve,) deploy; return the terminal deployment. Real Jenkins, real worker."""

    token = context["tokens"]["admin"]
    module = context["module"]
    run = start_module_run(api, token, module, environment, context["commit"], f"acceptance-{label}-{int(time.time())}")
    application_id = run["applicationId"]

    def built():
        _, current = api.call("GET", f"/pipeline-runs/{run['id']}", token)
        if current.get("status") in {"failed", "cancelled"}:
            raise RuntimeError(f"CI run {run['id']} ended {current['status']} without a deployment")
        return current if current.get("artifactDigest") else None

    built_run = wait_until("the Jenkins build to publish a digest", built, timeout=900)

    def deployment_exists():
        return deployment_for_run(api, token, application_id, run["id"])

    deployment = wait_until("a deployment record", deployment_exists, timeout=120)
    if deployment["status"] == "pending_approval":
        if not approve_with:
            raise RuntimeError("deployment needs approval and no approver token was given")
        status, body = api.call("POST", f"/deployments/{deployment['id']}/approve", approve_with, {"comment": "acceptance harness"})
        if status != 202:
            raise RuntimeError(f"approval refused: {status} {body}")

    def terminal():
        current = deployment_for_run(api, token, application_id, run["id"])
        return current if current and current["status"] in {"healthy", "failed", "rolled_back", "rollback_failed", "cancelled"} else None

    final = wait_until("the deployment to reach a terminal state", terminal, timeout=600)
    final["_run"] = built_run
    return final


# -------------------------------------------------------------------- gates


@gate("persistence_across_api_restart")
def gate_persistence(context):
    """A row written through one connection is read through another, and the running
    API -- a separate process -- serves what the database holds, not a cache."""

    db_url = os.getenv("DATABASE_URL", "").strip()
    if not db_url:
        raise Blocked("DATABASE_URL is not set")
    from app.store.postgres import PostgresDatabase
    from app.store.records import SystemRow

    api: Api = context["api"]
    token = context["tokens"]["admin"]
    system_id = f"sys-persist-{int(time.time())}"
    with PostgresDatabase(db_url).transaction() as tx:
        tx.insert_portal_system(SystemRow(id=system_id, unit="Acceptance", description="harness", owner="admin", status="healthy"))
    try:
        status, body = api.call("GET", f"/systems/{system_id}", token)
        if status != 200:
            raise RuntimeError(f"the API did not serve a row written directly to PostgreSQL: {status} {body}")
    finally:
        with PostgresDatabase(db_url).transaction() as tx:
            tx.delete_portal_system(system_id)
    status, _ = api.call("GET", f"/systems/{system_id}", token)
    if status != 404:
        raise RuntimeError("the API still served a row after it was deleted from PostgreSQL")
    return "a row written to PostgreSQL was served by the running API and gone once deleted", {"systemId": system_id}


@gate("oidc_role_and_team")
def gate_oidc(context):
    """Three real identities from the IdP; the server maps their groups to roles and
    teams and enforces them: a developer may not start a production run."""

    api: Api = context["api"]
    who: dict[str, Any] = {}
    for role, expected in (("admin", "platform-admin"), ("reviewer", "reviewer"), ("developer", "developer")):
        status, me = api.call("GET", "/me", context["tokens"][role])
        if status != 200:
            raise RuntimeError(f"/me refused the {role} token: {status} {me}")
        principal = me["principal"]
        if principal.get("method") != "oidc":
            raise RuntimeError(f"{role} authenticated by {principal.get('method')!r}, not oidc")
        if expected not in principal.get("roles", []):
            raise RuntimeError(f"{role} identity does not carry the {expected} role: {principal.get('roles')}")
        if not principal.get("teams"):
            raise RuntimeError(f"{role} identity carries no team claim")
        who[role] = {"subject": principal["subject"], "roles": principal["roles"], "teams": principal["teams"]}
    status, body = api.call("GET", "/me", "not-a-token")
    if status != 401:
        raise RuntimeError(f"a garbage token was answered {status}")
    status, body = api.call("POST", f"/modules/{context['module']}/pipeline-runs", context["tokens"]["developer"],
                            {"commitSha": context["commit"], "branch": "main", "environment": "prod"},
                            headers={"Idempotency-Key": f"acceptance-dev-prod-{int(time.time())}"})
    if status != 403 or (body or {}).get("code") != "ENVIRONMENT_FORBIDDEN":
        raise RuntimeError(f"a developer starting a production run was answered {status} {body}")
    return "three IdP identities mapped to platform-admin/reviewer/developer with teams; developer refused for prod", who


@gate("dcim_inventory_lookup")
def gate_dcim(context):
    """The inventory answers from the real DCIM, and a retired device is reported as such."""

    api: Api = context["api"]
    token = context["tokens"]["admin"]
    status, ready = api.call("GET", "/readyz")
    dcim = (ready or {}).get("dcim", {})
    if dcim.get("status") == "not_configured":
        raise Blocked("no DCIM provider is configured")
    if not dcim.get("ready"):
        raise RuntimeError(f"DCIM is not ready: {dcim}")
    status, module = api.call("GET", f"/modules/{context['module']}", token)
    if status != 200:
        raise RuntimeError(f"module lookup failed: {status}")
    status, servers = api.call("GET", f"/dcim/servers?systemId={module['systemId']}&moduleId={module['id']}", token)
    if status != 200:
        raise RuntimeError(f"/dcim/servers answered {status}: {servers}")
    items = servers.get("items", servers) if isinstance(servers, dict) else servers
    names = {item.get("name") or item.get("hostname") for item in items}
    retired = _require_env("NETCI_ACCEPTANCE_RETIRED_SERVER")
    match = next((item for item in items if (item.get("name") or item.get("hostname")) == retired), None)
    if match is None:
        raise RuntimeError(f"{retired} is not in the DCIM inventory for this module: {sorted(names)}")
    if str(match.get("status", "")).lower() not in {"decommissioning", "decommissioned", "offline", "failed"}:
        raise RuntimeError(f"{retired} is not in a blocking state in DCIM: {match.get('status')}")
    return f"{len(items)} devices from {dcim.get('source')}; {retired} reported {match.get('status')}", {
        "source": dcim.get("source"), "devices": sorted(n for n in names if n), "retired": match,
    }


@gate("jenkins_checkout_build_push")
def gate_jenkins(context):
    """A real build: Jenkins checks out the commit, builds, pushes, and netCI records the
    digest and the controller that did it. The deployment that follows is left for the
    Temporal gate; this one ends when the artifact exists."""

    api: Api = context["api"]
    status, ready = api.call("GET", "/readyz")
    ci = (ready or {}).get("ci", {})
    if ci.get("mode") != "jenkins":
        raise Blocked("CI mode is not jenkins")
    if not ci.get("ready"):
        raise RuntimeError(f"no healthy Jenkins controller: {ci}")
    deployment = release(api, context, "dev", "jenkins")
    run = deployment["_run"]
    context["dev_release"] = deployment
    status, evidence = api.call("GET", f"/pipeline-runs/{run['id']}/security-evidence", context["tokens"]["admin"])
    if status != 200:
        raise RuntimeError(f"no security evidence for the run: {status}")
    context["dev_evidence"] = evidence
    if evidence.get("artifactDigest") != run["artifactDigest"]:
        raise RuntimeError("evidence digest does not match the run's digest")
    return f"{run['jenkinsRunId']} built {run['commitSha'][:12]} -> {run['artifactDigest'][:19]}...", {
        "pipelineRunId": run["id"], "jenkinsRunId": run["jenkinsRunId"], "artifactDigest": run["artifactDigest"],
        "artifactRef": evidence.get("artifactRef"), "deploymentStatus": deployment["status"],
    }


@gate("sbom_trivy_cosign_verification")
def gate_cosign(context):
    """The evidence CI wrote is checked, then cosign is run here, independently, against
    the digest -- and against a digest that was never signed, which must fail."""

    evidence = context.get("dev_evidence")
    if not evidence:
        raise Blocked("no built artifact to verify (the Jenkins gate did not produce one)")
    sbom = evidence.get("sbom") or {}
    scan = evidence.get("vulnerabilityScan") or evidence.get("scan") or {}
    signature = evidence.get("signature") or {}
    if not sbom.get("location"):
        raise RuntimeError("evidence carries no SBOM location")
    if scan.get("scanner") != "trivy" or scan.get("status") != "passed":
        raise RuntimeError(f"vulnerability scan is not a passed trivy scan: {scan}")
    if signature.get("provider") != "cosign" or not signature.get("verified"):
        raise RuntimeError(f"signature evidence is not a verified cosign signature: {signature}")
    executable = os.getenv("NETCI_COSIGN_EXECUTABLE", "cosign").strip() or "cosign"
    key = os.getenv("NETCI_COSIGN_PUBLIC_KEY_FILE", "").strip()
    if not key:
        raise Blocked("NETCI_COSIGN_PUBLIC_KEY_FILE is not set; cannot verify independently")
    reference = str(evidence.get("artifactRef") or "")
    if "@sha256:" not in reference:
        raise RuntimeError(f"artifactRef is not digest-pinned: {reference!r}")
    base = [executable, "verify", "--key", key, "--insecure-ignore-tlog=true"]
    genuine = subprocess.run([*base, reference], capture_output=True, text=True, timeout=120)
    if genuine.returncode != 0:
        raise RuntimeError(f"cosign refused the genuine artifact: {genuine.stderr.strip()[-400:]}")
    unsigned = reference.split("@", 1)[0] + "@sha256:" + "0" * 64
    bogus = subprocess.run([*base, unsigned], capture_output=True, text=True, timeout=120)
    if bogus.returncode == 0:
        raise RuntimeError("cosign accepted a digest nothing ever signed")
    version = subprocess.run([executable, "version", "--json"], capture_output=True, text=True, timeout=10)
    try:
        cosign_version = json.loads(version.stdout).get("gitVersion")
    except ValueError:
        cosign_version = None
    return f"syft SBOM, trivy passed, cosign {cosign_version} verified {reference.split('@')[1][:19]}... and refused an unsigned digest", {
        "sbom": {"generatedBy": sbom.get("generatedBy"), "format": sbom.get("format")},
        "scan": {"critical": scan.get("critical"), "high": scan.get("high")},
        "cosignVersion": cosign_version, "artifactRef": reference,
    }


@gate("temporal_workflow_restart_resume")
def gate_temporal(context):
    """The worker is stopped mid-deployment and started again; the deployment must
    finish after the restart, and readiness must have said no_workers in between."""

    api: Api = context["api"]
    status, ready = api.call("GET", "/readyz")
    cd = (ready or {}).get("cd", {})
    if cd.get("mode") != "temporal":
        raise Blocked("CD mode is not temporal")
    restart = os.getenv("NETCI_ACCEPTANCE_WORKER_RESTART", "").strip()
    if not restart:
        raise Blocked("NETCI_ACCEPTANCE_WORKER_RESTART is not set: the harness cannot stop and restart the worker")
    if not cd.get("ready"):
        raise RuntimeError(f"CD is not ready before the drill: {cd}")
    token = context["tokens"]["admin"]
    # The restart command's first argument is "stop" or "start".
    subprocess.run([*shlex.split(restart), "stop"], check=True, timeout=60)
    try:
        def no_workers():
            _, current = api.call("GET", "/readyz")
            return current if (current or {}).get("cd", {}).get("status") == "no_workers" else None
        without = wait_until("readiness to report no_workers", no_workers, timeout=120, interval=5)
        # Start a run now: the build happens in Jenkins, the deployment then waits on a
        # queue that nobody polls.
        run = start_module_run(api, token, context["module"], "dev", context["commit"], f"acceptance-temporal-{int(time.time())}")
        def built():
            _, current = api.call("GET", f"/pipeline-runs/{run['id']}", token)
            if current.get("status") in {"failed", "cancelled"}:
                raise RuntimeError(f"CI run ended {current['status']}")
            return current if current.get("artifactDigest") else None
        wait_until("the Jenkins build", built, timeout=900)
        wait_until("a deployment record", lambda: deployment_for_run(api, token, run["applicationId"], run["id"]), timeout=120)
        time.sleep(20)
        stalled = deployment_for_run(api, token, run["applicationId"], run["id"])
        if stalled["status"] != "deploying":
            raise RuntimeError(f"with no worker the deployment should be stuck in deploying, found {stalled['status']}")
    finally:
        subprocess.run([*shlex.split(restart), "start"], check=True, timeout=60)
    def workers_back():
        _, current = api.call("GET", "/readyz")
        return current if (current or {}).get("cd", {}).get("ready") else None
    wait_until("readiness to report workers again", workers_back, timeout=120, interval=5)
    def terminal():
        current = deployment_for_run(api, token, run["applicationId"], run["id"])
        return current if current["status"] in {"healthy", "failed", "rolled_back", "rollback_failed"} else None
    final = wait_until("the deployment to finish after the restart", terminal, timeout=600)
    if final["status"] != "healthy":
        raise RuntimeError(f"the deployment finished {final['status']} after the worker restart")
    context["dev_release"] = final
    return "worker stopped (readiness: no_workers), deployment queued, worker restarted, deployment healthy", {
        "deploymentId": final["id"], "pipelineRunId": run["id"], "readinessWithoutWorker": without.get("cd"),
    }


@gate("ansible_host_and_target_namespace")
def gate_targets(context):
    """The server decides the target. A caller naming one is refused with 422, and a
    reviewed revision naming a retired DCIM device is refused at dispatch."""

    api: Api = context["api"]
    token = context["tokens"]["admin"]
    module = context["module"]
    status, body = api.call("POST", f"/modules/{module}/pipeline-runs", token,
                            {"commitSha": context["commit"], "branch": "main", "environment": "dev",
                             "parameters": {"target_hosts": ["attacker-host"], "target_namespace": "kube-system"}},
                            headers={"Idempotency-Key": f"acceptance-target-{int(time.time())}"})
    # Either refusal is the right one: the key is server-owned. What matters is that it
    # was refused loudly (422 naming the key), not dropped and quietly overridden.
    if status != 422 or (body or {}).get("code") not in {"BUILD_INPUT_NOT_ALLOWED", "DEPLOYMENT_PARAMETER_NOT_ACCEPTED"}:
        raise RuntimeError(f"caller-supplied target was answered {status} {body}")
    caller_refusal = body
    retired = _require_env("NETCI_ACCEPTANCE_RETIRED_SERVER")
    status, revisions = api.call("GET", f"/modules/{module}/config-revisions", token)
    active = next(r for r in revisions["items"] if r["active"])
    original = active["deploymentConfig"]
    poisoned = [dict(item, servers=[retired]) if item.get("environment") == "dev" else item for item in original]
    status, proposed = api.call("POST", f"/modules/{module}/config-revisions", token,
                                {"changeSummary": "acceptance: point dev at a retired device", "deploymentConfig": poisoned})
    if status != 201 or proposed.get("status") != "active":
        raise RuntimeError(f"could not activate the poisoned dev revision: {status} {proposed}")
    try:
        status, body = api.call("POST", f"/modules/{module}/pipeline-runs", token,
                                {"commitSha": context["commit"], "branch": "main", "environment": "dev"},
                                headers={"Idempotency-Key": f"acceptance-retired-{int(time.time())}"})
        if status != 422 or (body or {}).get("code") != "DCIM_TARGET_UNAVAILABLE":
            raise RuntimeError(f"dispatch to a retired DCIM device was answered {status} {body}")
        refusal = body
    finally:
        status, restored = api.call("POST", f"/modules/{module}/config-revisions", token,
                                    {"changeSummary": "acceptance: restore dev target", "deploymentConfig": original})
        if status != 201 or restored.get("status") != "active":
            raise RuntimeError(f"could not restore the dev target: {status} {restored}")
    return f"caller target refused (422 {caller_refusal.get('code')}); dispatch to {retired} refused (422 DCIM_TARGET_UNAVAILABLE)", {
        "callerRefusal": caller_refusal.get("message"), "dcimRefusal": refusal.get("message"),
        "restoredRevision": restored.get("revisionNumber"),
    }


@gate("deployment_failure_and_rollback")
def gate_rollback(context):
    """Roll the dev release back to the digest before it, through the API; the worker
    must run it and the platform must record `rolled_back` with the older digest live."""

    api: Api = context["api"]
    token = context["tokens"]["admin"]
    reviewer = context["tokens"]["reviewer"]
    current = context.get("dev_release")
    if not current or current.get("status") != "healthy":
        raise Blocked("no healthy dev release to roll back (an earlier gate did not produce one)")
    previous = current.get("previousArtifactDigest")
    if not previous:
        # Build once more so there is something to roll back to.
        newer = release(api, context, "dev", "rollback-second")
        if newer["status"] != "healthy":
            raise RuntimeError(f"the second release ended {newer['status']}")
        previous, current = current["artifactDigest"], newer
        if current.get("previousArtifactDigest") != previous:
            raise RuntimeError("the second release does not record the first as its previous digest")
    status, started = api.call("POST", f"/deployments/{current['id']}/rollback", reviewer,
                               {"targetArtifactDigest": previous, "reason": "acceptance harness rollback drill"})
    if status != 202 or started.get("status") != "rollback_in_progress":
        raise RuntimeError(f"rollback was not started: {status} {started}")
    def done():
        _, now = api.call("GET", f"/deployments/{current['id']}", token)
        return now if now.get("status") in {"rolled_back", "rollback_failed"} else None
    final = wait_until("the rollback to be reported", done, timeout=600, interval=5)
    if final["status"] != "rolled_back":
        raise RuntimeError(f"rollback ended {final['status']}")
    if final["artifactDigest"] != previous:
        raise RuntimeError("the deployment does not carry the rolled-back digest")
    context["dev_release"] = final
    return f"rolled back {current['artifactDigest'][:19]}... -> {previous[:19]}...; recorded rolled_back", {
        "deploymentId": final["id"], "from": current["artifactDigest"], "to": previous,
    }


@gate("backup_and_restore_verification")
def gate_backup(context):
    db_url = os.getenv("DATABASE_URL", "").strip()
    if not db_url:
        raise Blocked("DATABASE_URL is not set")
    from scripts.netci_backup import drill

    exit_code = drill(argparse.Namespace(database_url=db_url))
    if exit_code != 0:
        raise RuntimeError(f"backup drill exited {exit_code}")
    return "backup drill: dump, restore into a scratch database, row counts and checksums compared, failure injection detected", {"exitCode": exit_code}


GATES = [gate_persistence, gate_oidc, gate_dcim, gate_jenkins, gate_cosign, gate_temporal, gate_targets, gate_rollback, gate_backup]


# --------------------------------------------------------------------- main


def main() -> int:
    print("=" * 63)
    print("           netCI Production Acceptance Harness (P0)            ")
    print("=" * 63)
    commit = git_commit_sha()
    started_at = datetime.now(timezone.utc)
    context: dict[str, Any] = {"commit": os.getenv("NETCI_ACCEPTANCE_COMMIT", "").strip(), "module": os.getenv("NETCI_ACCEPTANCE_MODULE", "").strip()}
    api_url = os.getenv("NETCI_ACCEPTANCE_API_URL", "").strip()
    print(f"Commit under test: {commit}")
    print(f"API:               {api_url or '(not set)'}")
    print(f"Timestamp:         {started_at.isoformat()}")
    print("-" * 63)

    gates: list[GateResult] = []
    if not api_url or not context["module"] or not context["commit"]:
        reason = "NETCI_ACCEPTANCE_API_URL, NETCI_ACCEPTANCE_MODULE and NETCI_ACCEPTANCE_COMMIT must all be set"
        gates = [GateResult(g.gate_name, "BLOCKED", reason, 0.0) for g in GATES]
    else:
        context["api"] = Api(api_url)
        try:
            context["tokens"] = tokens()
        except Blocked as exc:
            gates = [GateResult(g.gate_name, "BLOCKED", str(exc), 0.0) for g in GATES]
        except Exception as exc:  # noqa: BLE001
            gates = [GateResult(g.gate_name, "FAIL", f"identity acquisition failed: {exc}", 0.0) for g in GATES]
        if not gates:
            for run_gate in GATES:
                result = run_gate(context)
                print(f"{result.gate:<38} {result.status:<8} {result.reason}")
                gates.append(result)

    passed = sum(1 for g in gates if g.status == "PASS")
    failed = sum(1 for g in gates if g.status == "FAIL")
    blocked = sum(1 for g in gates if g.status == "BLOCKED")
    print("-" * 63)
    print(f"Summary: {passed} PASS, {failed} FAIL, {blocked} BLOCKED (Total: {len(gates)})")

    # Tests and dry runs point this elsewhere so a BLOCKED rehearsal never overwrites
    # the evidence of a real run.
    evidence_dir = Path(os.getenv("NETCI_ACCEPTANCE_EVIDENCE_DIR", str(ROOT / "evidence")))
    evidence_dir.mkdir(parents=True, exist_ok=True)
    verdict = "FAIL" if failed else ("BLOCKED" if blocked else "PASS")
    report = {
        "timestamp": started_at.isoformat(),
        "finishedAt": datetime.now(timezone.utc).isoformat(),
        "commitSha": commit,
        "apiUrl": api_url,
        "module": context["module"],
        "buildCommit": context["commit"],
        "summary": {"pass": passed, "fail": failed, "blocked": blocked, "total": len(gates)},
        "verdict": verdict,
        "gates": [asdict(g) for g in gates],
    }
    stamp = started_at.strftime("%Y%m%dT%H%M%SZ")
    evidence_file = evidence_dir / f"production_acceptance_{stamp}.json"
    evidence_file.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    junit = ['<?xml version="1.0" encoding="UTF-8"?>',
             f'<testsuite name="production_acceptance" tests="{len(gates)}" failures="{failed}" skipped="{blocked}" timestamp="{started_at.isoformat()}">']
    for g in gates:
        junit.append(f'  <testcase name="{g.gate}" classname="acceptance" time="{g.duration_seconds:.3f}">')
        if g.status == "FAIL":
            junit.append(f'    <failure message="{_xml(g.reason)}">{_xml(json.dumps(g.details, default=str))}</failure>')
        elif g.status == "BLOCKED":
            junit.append(f'    <skipped message="{_xml(g.reason)}"/>')
        junit.append("  </testcase>")
    junit.append("</testsuite>")
    (evidence_dir / "acceptance.xml").write_text("\n".join(junit) + "\n", encoding="utf-8")
    print(f"Evidence: {evidence_file}")
    print(f"Verdict:  {verdict}")
    return 1 if failed else 0


def _xml(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


if __name__ == "__main__":
    raise SystemExit(main())
