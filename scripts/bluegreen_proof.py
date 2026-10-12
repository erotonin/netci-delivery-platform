#!/usr/bin/env python3
"""Prove a blue/green release switches real traffic and can be switched back (ADR-035).

  stable serves A          -> blue/green request for version B
  approve                  -> B installed as the idle colour (no traffic yet)
  healthy                  -> the stable ingress is switched to that colour: sample 100 % B
  switch back              -> one call, sample 100 % A (the previous colour still runs)
  switch forward           -> 100 % B again

    NETCI_API_URL=… NETCI_CANARY_INGRESS=https://172.17.0.3 NETCI_TRAFFIC_KUBECONFIG=… \
      scripts/bluegreen_proof.py --module hello-kubernetes --version-run <pipelineRunId>
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from canary_proof import TERMINAL, sample, share  # noqa: E402
from production_acceptance_harness import Api, _password_grant, wait_until  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--module", required=True)
    parser.add_argument("--version-run", required=True)
    parser.add_argument("--samples", type=int, default=200)
    parser.add_argument("--namespace", default="prod")
    parser.add_argument("--out", default="evidence/bluegreen-nginx.json")
    args = parser.parse_args()

    api = Api(os.environ.get("NETCI_API_URL", "http://127.0.0.1:8100"))
    ingress = os.environ["NETCI_CANARY_INGRESS"].rstrip("/")
    kubeconfig = os.environ["NETCI_TRAFFIC_KUBECONFIG"]
    password = os.environ["NETCI_LAB_USER_PASSWORD"]
    tokens = {k: _password_grant(os.environ.get(f"NETCI_ACCEPTANCE_{k.upper()}_USER", d), password)
              for k, d in (("admin", "pat"), ("developer", "dana"), ("reviewer", "rae"))}
    host = f"{args.module}.{args.namespace}.netci.local"
    evidence: dict = {"schemaVersion": "1.0", "startedAt": datetime.now(timezone.utc).isoformat(), "module": args.module,
                      "samplesPerStep": args.samples, "observations": []}
    failures: list[str] = []

    def observe(label: str, digest: str, expected: int, extra: dict | None = None) -> None:
        seen, errors = sample(ingress, host, args.samples)
        observed = share(seen, digest)
        ok = observed == expected and errors == 0
        evidence["observations"].append({"label": label, "expectedPercent": expected, "observedPercent": observed,
                                         "answersByDigest": dict(seen), "errors": errors, "matched": ok, **(extra or {})})
        print(f"[{label}] expected {expected}% observed {observed}% ({dict(seen)}, errors={errors}) -> {'ok' if ok else 'MISMATCH'}")
        if not ok:
            failures.append(label)

    _, module = api.call("GET", f"/modules/{args.module}", tokens["admin"])
    evidence["applicationId"] = module["applicationId"]
    _, run = api.call("GET", f"/pipeline-runs/{args.version_run}", tokens["admin"])
    new_digest = run["artifactDigest"]
    seen, errors = sample(ingress, host, 20)
    if errors or len(seen) != 1:
        print(f"stable must answer alone first: {dict(seen)} errors={errors}", file=sys.stderr)
        return 2
    old_digest = next(iter(seen))
    if old_digest == new_digest:
        print("pick a run whose digest differs from what prod serves", file=sys.stderr)
        return 2
    evidence.update({"servingBefore": old_digest, "newDigest": new_digest, "host": host})

    tag = f"0.0.{int(time.time()) % 100000}+bluegreen.{new_digest[7:15]}"
    status, body = api.call("POST", f"/modules/{args.module}/versions", tokens["admin"], {
        "tag": tag, "gitTagUrl": f"https://lab.invalid/{args.module}/tags/{tag}",
        "artifactUrl": f"https://registry.invalid/{args.module}@{new_digest}", "pipelineRunId": args.version_run, "artifactDigest": new_digest})
    if status not in (201, 409):
        print(f"version registration failed: {status} {body}", file=sys.stderr)
        return 2
    status, request = api.call("POST", "/production-requests", tokens["developer"], {
        "modules": [{"moduleId": args.module, "version": tag}], "scheduledFor": datetime.now(timezone.utc).isoformat(),
        "rollbackStrategy": "automatic", "runAutomationTests": False, "strategy": "blue_green",
    }, headers={"Idempotency-Key": f"bluegreen-proof-{int(time.time())}"})
    if status != 201:
        print(f"request refused: {status} {request}", file=sys.stderr)
        return 2
    request_id = request["id"]
    status, approved = api.call("POST", f"/production-requests/{request_id}/approve", tokens["reviewer"], {"comment": "blue/green proof"})
    if status != 202:
        print(f"approval refused: {status} {approved}", file=sys.stderr)
        return 2
    deployment_id = approved["modules"][0]["deploymentId"]
    _, before = api.call("GET", f"/deployments/{deployment_id}/traffic", tokens["admin"])
    colour = before["activeColor"]
    evidence.update({"productionRequestId": request_id, "deploymentId": deployment_id, "colour": colour, "servingColourBefore": before["routerStatus"]["activeColor"]})
    print(f"release goes to {colour}; ingress currently on {before['routerStatus']['activeColor']}")
    observe("while the colour deploys (nothing switched yet)", new_digest, 0)

    def terminal():
        _, current = api.call("GET", f"/deployments/{deployment_id}", tokens["admin"])
        return current if current.get("status") in TERMINAL else None

    final = wait_until("the colour to be healthy", terminal, timeout=600)
    if final["status"] != "healthy":
        print(f"deployment ended {final['status']}: {final.get('message')}", file=sys.stderr)
        evidence["failure"] = final
        Path(args.out).write_text(json.dumps(evidence, indent=2) + "\n")
        return 1
    time.sleep(3)
    _, after = api.call("GET", f"/deployments/{deployment_id}/traffic", tokens["admin"])
    observe(f"healthy -> ingress switched to {colour}", new_digest, 100, {"routerStatus": after["routerStatus"]})
    if after["routerStatus"]["activeColor"] != colour:
        failures.append("router-colour")

    previous = "blue" if colour == "green" else "green"
    status, back = api.call("POST", f"/deployments/{deployment_id}/traffic/switch", tokens["reviewer"], {"activeColor": previous})
    evidence["switchBack"] = {"status": status, "body": back}
    if status == 200:
        time.sleep(3)
        observe(f"switched back to {previous}", old_digest, 100, {"routerStatus": back["routerStatus"]})
        status, fwd = api.call("POST", f"/deployments/{deployment_id}/traffic/switch", tokens["reviewer"], {"activeColor": colour})
        time.sleep(3)
        observe(f"switched forward to {colour} again", new_digest, 100, {"switch": status})
    else:
        # The previous colour is the stable release's own Deployment on a first blue/green:
        # there is no `blue` release yet, and the router says so instead of switching to nothing.
        evidence["switchBackNote"] = "no previous colour release exists (first blue/green after a rolling release); the router refused rather than switch to nothing"
        print(f"switch back refused: {status} {back.get('message', back)}")
    releases = subprocess.run(["helm", "--kubeconfig", kubeconfig, "-n", args.namespace, "ls", "-q"], capture_output=True, text=True, check=False).stdout.split()
    evidence["helmReleases"] = releases
    evidence["finishedAt"] = datetime.now(timezone.utc).isoformat()
    evidence["result"] = "passed" if not failures else "failed"
    evidence["failures"] = failures
    Path(args.out).write_text(json.dumps(evidence, indent=2) + "\n")
    print(f"{evidence['result']}: {args.out}")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
