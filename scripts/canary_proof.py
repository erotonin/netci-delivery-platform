#!/usr/bin/env python3
"""Prove a canary release moves real traffic (ADR-031).

Drives one canary production request end to end against a running netCI and samples
the application's ingress between steps, so every weight netCI reports is checked
against what the ingress controller actually did:

  stable serves A           -> register version B, request canary [w1, w2, 100]
  approve                   -> canary release B installed beside A at w1 %
  sample N requests         -> ~w1 % answer with B's digest
  advance                   -> w2 %; sample again
  advance                   -> 100 %; a promote deployment moves stable to B and
                               removes the canary release; sample: 100 % B, no canary
                               ingress left

Tokens come from the same OIDC password grant as the acceptance harness (lab only).
Writes evidence/canary-nginx.json. Exit 0 only when every observation matched.

    NETCI_API_URL=http://127.0.0.1:8100 NETCI_CANARY_INGRESS=https://172.17.0.3 \
      scripts/canary_proof.py --module hello-kubernetes --version-run <pipelineRunId>
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from production_acceptance_harness import Api, _password_grant, wait_until  # noqa: E402

TERMINAL = {"healthy", "failed", "rolled_back", "rollback_failed", "cancelled"}


def sample(ingress: str, host: str, count: int) -> tuple[Counter, int]:
    """Send `count` requests through the ingress; count answers by the digest they report."""

    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE  # the lab has no cert-manager; the proof is about routing
    seen: Counter = Counter()
    errors = 0
    for _ in range(count):
        request = urllib.request.Request(f"{ingress}/", headers={"Host": host})
        try:
            with urllib.request.urlopen(request, timeout=5, context=context) as response:
                seen[json.loads(response.read())["version"]] += 1
        except (urllib.error.URLError, ValueError, KeyError, TimeoutError):
            errors += 1
    return seen, errors


def share(seen: Counter, digest: str) -> float:
    total = sum(seen.values())
    return round(100.0 * seen.get(digest, 0) / total, 1) if total else 0.0


def kubectl(kubeconfig: str, *args: str) -> Any:
    out = subprocess.run(["kubectl", "--kubeconfig", kubeconfig, *args, "-o", "json"], capture_output=True, text=True, check=False)
    return json.loads(out.stdout) if out.returncode == 0 and out.stdout else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--module", required=True)
    parser.add_argument("--version-run", required=True, help="a succeeded pipeline run whose digest differs from what prod serves")
    parser.add_argument("--version-tag", default=None)
    parser.add_argument("--steps", default="20,50,100")
    parser.add_argument("--samples", type=int, default=200)
    parser.add_argument("--tolerance", type=float, default=12.0, help="percentage points a sampled share may differ from the weight")
    parser.add_argument("--namespace", default="prod")
    parser.add_argument("--out", default="evidence/canary-nginx.json")
    parser.add_argument("--abort-after-first-step", action="store_true",
                        help="prove the abort path instead: after the first weight, abort and check stable is untouched")
    args = parser.parse_args()

    api = Api(os.environ.get("NETCI_API_URL", "http://127.0.0.1:8100"))
    ingress = os.environ["NETCI_CANARY_INGRESS"].rstrip("/")
    kubeconfig = os.environ["NETCI_TRAFFIC_KUBECONFIG"]
    password = os.environ["NETCI_LAB_USER_PASSWORD"]
    tokens = {
        "admin": _password_grant(os.environ.get("NETCI_ACCEPTANCE_ADMIN_USER", "pat"), password),
        "developer": _password_grant(os.environ.get("NETCI_ACCEPTANCE_DEVELOPER_USER", "dana"), password),
        "reviewer": _password_grant(os.environ.get("NETCI_ACCEPTANCE_REVIEWER_USER", "rae"), password),
    }
    steps = [int(s) for s in args.steps.split(",")]
    evidence: dict[str, Any] = {
        "schemaVersion": "1.0", "startedAt": datetime.now(timezone.utc).isoformat(), "module": args.module,
        "steps": steps, "samplesPerStep": args.samples, "tolerancePercentagePoints": args.tolerance, "observations": [],
    }
    failures: list[str] = []

    def observe(label: str, expected_weight: int, digest: str, host: str, extra: dict[str, Any] | None = None) -> None:
        seen, errors = sample(ingress, host, args.samples)
        observed = share(seen, digest)
        ok = abs(observed - expected_weight) <= args.tolerance and errors == 0
        evidence["observations"].append({
            "label": label, "expectedCanaryPercent": expected_weight, "observedCanaryPercent": observed,
            "answersByDigest": dict(seen), "errors": errors, "matched": ok, **(extra or {}),
        })
        print(f"[{label}] expected {expected_weight}% canary, observed {observed}% ({dict(seen)}, errors={errors}) -> {'ok' if ok else 'MISMATCH'}")
        if not ok:
            failures.append(label)

    # ---- what prod serves now, and the version to canary
    _, module = api.call("GET", f"/modules/{args.module}", tokens["admin"])
    application_id = module["applicationId"]
    _, run = api.call("GET", f"/pipeline-runs/{args.version_run}", tokens["admin"])
    if run.get("status") != "succeeded" or not run.get("artifactDigest"):
        print(f"run {args.version_run} is {run.get('status')} without a digest", file=sys.stderr)
        return 2
    canary_digest = run["artifactDigest"]
    host = f"{args.module}.{args.namespace}.netci.local"
    baseline_seen, errors = sample(ingress, host, 20)
    if errors or len(baseline_seen) != 1:
        print(f"stable must answer alone before the canary: {dict(baseline_seen)} errors={errors}", file=sys.stderr)
        return 2
    stable_digest = next(iter(baseline_seen))
    if stable_digest == canary_digest:
        print("the version to canary is what prod already serves; pick another run", file=sys.stderr)
        return 2
    evidence.update({"stableDigest": stable_digest, "canaryDigest": canary_digest, "host": host, "ingress": ingress})
    print(f"stable serves {stable_digest[:19]}…, canary will be {canary_digest[:19]}…")

    # ---- register the version (idempotent by tag) and request a canary release
    # Semver is the version contract; the build number keeps a re-run of the proof
    # from colliding with an earlier registration of another digest.
    tag = args.version_tag or f"0.0.{int(time.time()) % 100000}+canary.{canary_digest[7:15]}"
    status, body = api.call("POST", f"/modules/{args.module}/versions", tokens["admin"], {
        "tag": tag, "gitTagUrl": f"https://lab.invalid/{args.module}/tags/{tag}",
        "artifactUrl": f"https://registry.invalid/{args.module}@{canary_digest}",
        "pipelineRunId": args.version_run, "artifactDigest": canary_digest,
    })
    if status not in (201, 409):
        print(f"version registration failed: {status} {body}", file=sys.stderr)
        return 2
    status, request = api.call("POST", "/production-requests", tokens["developer"], {
        "modules": [{"moduleId": args.module, "version": tag}],
        "scheduledFor": datetime.now(timezone.utc).isoformat(),
        "rollbackStrategy": "automatic", "runAutomationTests": False,
        "strategy": "canary", "strategyConfig": {"steps": steps},
    }, headers={"Idempotency-Key": f"canary-proof-{int(time.time())}"})
    if status != 201:
        print(f"production request refused: {status} {request}", file=sys.stderr)
        return 2
    request_id = request["id"]
    evidence["productionRequestId"] = request_id
    status, approved = api.call("POST", f"/production-requests/{request_id}/approve", tokens["reviewer"], {"comment": "canary proof"})
    if status != 202:
        print(f"approval refused: {status} {approved}", file=sys.stderr)
        return 2
    deployment_id = approved["modules"][0]["deploymentId"]
    evidence["canaryDeploymentId"] = deployment_id

    def terminal(dep_id: str):
        _, current = api.call("GET", f"/deployments/{dep_id}", tokens["admin"])
        return current if current.get("status") in TERMINAL else None

    canary = wait_until("the canary deployment to finish", lambda: terminal(deployment_id), timeout=600)
    if canary["status"] != "healthy":
        print(f"canary deployment ended {canary['status']}: {canary.get('message')}", file=sys.stderr)
        evidence["failure"] = canary
        Path(args.out).write_text(json.dumps(evidence, indent=2) + "\n")
        return 1
    _, traffic = api.call("GET", f"/deployments/{deployment_id}/traffic", tokens["admin"])
    print(f"canary healthy; router reports {traffic['routerStatus']}")
    time.sleep(3)  # nginx reloads its configuration after the ingress appears
    observe(f"step 1 ({steps[0]} %)", steps[0], canary_digest, host, {"routerStatus": traffic["routerStatus"]})

    if args.abort_after_first_step:
        status, aborted = api.call("POST", f"/production-requests/{request_id}/canary/abort", tokens["reviewer"],
                                   {"reason": "canary proof: operator abort"})
        evidence["abort"] = aborted
        if status != 200 or aborted.get("trafficWeight") != 0 or not aborted.get("canaryReleaseRetired"):
            failures.append("abort")
            print(f"abort did not retire the canary: {status} {aborted}", file=sys.stderr)
        else:
            time.sleep(2)
            observe("after abort (weight 0, before the canary release is gone)", 0, canary_digest, host)
            rolled = wait_until("the canary deployment to be rolled back", lambda: terminal(deployment_id), timeout=600)
            evidence["canaryDeploymentAfterAbort"] = {"id": rolled["id"], "status": rolled["status"]}
            if rolled["status"] != "rolled_back":
                failures.append("abort-rollback")
            time.sleep(3)
            canary_left = kubectl(kubeconfig, "get", "ingress", "-A", "-l", f"netci.io/application={application_id},netci.io/track=canary")
            releases = subprocess.run(["helm", "--kubeconfig", kubeconfig, "-n", args.namespace, "ls", "-q"], capture_output=True, text=True, check=False).stdout.split()
            stable_image = (kubectl(kubeconfig, "-n", args.namespace, "get", "deploy", f"{args.module}-sample-kubernetes-app") or {}).get("spec", {}).get("template", {}).get("spec", {}).get("containers", [{}])[0].get("image", "")
            observe("after abort (canary release removed, stable untouched)", 0, canary_digest, host, {
                "canaryIngressesLeft": len((canary_left or {}).get("items", [])), "helmReleases": releases, "stableImage": stable_image,
            })
            if (canary_left or {}).get("items") or f"{args.module}-canary" in releases:
                failures.append("canary-left-after-abort")
            if not stable_image.endswith(stable_digest):
                failures.append("stable-changed-by-abort")
        evidence["finishedAt"] = datetime.now(timezone.utc).isoformat()
        evidence["result"] = "passed" if not failures else "failed"
        evidence["failures"] = failures
        Path(args.out).write_text(json.dumps(evidence, indent=2) + "\n")
        print(f"{evidence['result']}: {args.out}")
        return 0 if not failures else 1

    # ---- middle steps
    for index, weight in enumerate(steps[1:-1], start=2):
        status, advanced = api.call("POST", f"/production-requests/{request_id}/canary/advance", tokens["reviewer"],
                                    {"metrics": {"errorRate": 0.0, "p95LatencyMs": 20.0}})
        if status != 200 or advanced.get("trafficWeight") != weight:
            print(f"advance to {weight} failed: {status} {advanced}", file=sys.stderr)
            failures.append(f"advance-{weight}")
            break
        _, traffic = api.call("GET", f"/deployments/{deployment_id}/traffic", tokens["admin"])
        time.sleep(2)
        observe(f"step {index} ({weight} %)", weight, canary_digest, host, {"routerStatus": traffic["routerStatus"], "advance": advanced})

    # ---- last step: 100 % and promotion
    status, promoted = api.call("POST", f"/production-requests/{request_id}/canary/advance", tokens["reviewer"],
                                {"metrics": {"errorRate": 0.0, "p95LatencyMs": 20.0}})
    evidence["promotion"] = promoted
    if status != 200 or promoted.get("status") != "promoting":
        print(f"promotion did not start: {status} {promoted}", file=sys.stderr)
        failures.append("promotion")
    else:
        time.sleep(2)
        observe("step 100 % (before promotion completes)", 100, canary_digest, host)
        promotion = wait_until("the promotion deployment to finish", lambda: terminal(promoted["promotionDeploymentId"]), timeout=600)
        evidence["promotionDeployment"] = {"id": promotion["id"], "status": promotion["status"], "artifactDigest": promotion["artifactDigest"]}
        if promotion["status"] != "healthy":
            failures.append("promotion-deployment")
            print(f"promotion deployment ended {promotion['status']}: {promotion.get('message')}", file=sys.stderr)
        else:
            time.sleep(3)
            canary_left = kubectl(kubeconfig, "get", "ingress", "-A", "-l", f"netci.io/application={application_id},netci.io/track=canary")
            releases = subprocess.run(["helm", "--kubeconfig", kubeconfig, "-n", args.namespace, "ls", "-q"], capture_output=True, text=True, check=False).stdout.split()
            stable_image = (kubectl(kubeconfig, "-n", args.namespace, "get", "deploy", f"{args.module}-sample-kubernetes-app") or {}).get("spec", {}).get("template", {}).get("spec", {}).get("containers", [{}])[0].get("image", "")
            _, traffic = api.call("GET", f"/deployments/{deployment_id}/traffic", tokens["admin"])
            observe("after promotion (stable serves the canary digest, canary release removed)", 100, canary_digest, host, {
                "canaryIngressesLeft": len((canary_left or {}).get("items", [])),
                "helmReleases": releases, "stableImage": stable_image, "routerStatus": traffic["routerStatus"],
            })
            if (canary_left or {}).get("items"):
                failures.append("canary-ingress-left")
            if f"{args.module}-canary" in releases:
                failures.append("canary-release-left")
            if not stable_image.endswith(canary_digest):
                failures.append("stable-not-promoted")

    evidence["finishedAt"] = datetime.now(timezone.utc).isoformat()
    evidence["result"] = "passed" if not failures else "failed"
    evidence["failures"] = failures
    Path(args.out).write_text(json.dumps(evidence, indent=2) + "\n")
    print(f"{evidence['result']}: {args.out}")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
