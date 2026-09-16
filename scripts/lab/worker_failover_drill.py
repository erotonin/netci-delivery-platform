#!/usr/bin/env python3
"""Kill the Temporal worker in the middle of a deployment; the other worker finishes it.

Lab drill for ADR-032. Two workers poll the same task queue. A dev build of the module is
started; when a worker is seen running the playbook for the resulting deployment, that
worker is killed with SIGKILL (no goodbye). Temporal notices through the missed activity
heartbeat and retries the `deploy` activity on the surviving worker. The drill records
how long the deployment stalled and which process finished it.

    scripts/lab/worker_failover_drill.py --module hello-kubernetes --environment dev
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from production_acceptance_harness import Api, _password_grant, wait_until  # noqa: E402

TERMINAL = {"healthy", "failed", "rolled_back", "rollback_failed", "cancelled"}


def worker_pids() -> dict[int, str]:
    out = subprocess.run(["pgrep", "-af", "[p]ython -m app.workflows.worker"], capture_output=True, text=True, check=False).stdout
    return {int(line.split()[0]): line for line in out.splitlines() if line.strip()}


def playbook_parent(deployment_id: str) -> int | None:
    """The worker pid whose ansible-playbook child carries this deployment id."""

    out = subprocess.run(["pgrep", "-af", "[a]nsible-playbook"], capture_output=True, text=True, check=False).stdout
    for line in out.splitlines():
        if deployment_id in line:
            pid = int(line.split()[0])
            parent = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True, check=False).stdout.strip()
            return int(parent) if parent else None
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--module", required=True)
    parser.add_argument("--environment", default="dev")
    parser.add_argument("--commit", default=None)
    parser.add_argument("--out", default="evidence/worker-failover.json")
    args = parser.parse_args()

    api = Api(os.environ.get("NETCI_API_URL", "http://127.0.0.1:8100"))
    admin = _password_grant(os.environ.get("NETCI_ACCEPTANCE_ADMIN_USER", "pat"), os.environ["NETCI_LAB_USER_PASSWORD"])
    commit = args.commit or os.environ["NETCI_ACCEPTANCE_COMMIT"]
    workers_before = worker_pids()
    if len(workers_before) < 2:
        print(f"need two workers, found {len(workers_before)}", file=sys.stderr)
        return 2
    evidence = {
        "schemaVersion": "1.0", "startedAt": datetime.now(timezone.utc).isoformat(), "module": args.module,
        "environment": args.environment, "workersBefore": sorted(workers_before),
    }

    status, run = api.call("POST", f"/modules/{args.module}/pipeline-runs", admin,
                           {"commitSha": commit, "branch": "main", "environment": args.environment},
                           headers={"Idempotency-Key": f"worker-drill-{int(time.time())}"})
    if status != 202:
        print(f"could not start the run: {status} {run}", file=sys.stderr)
        return 2
    evidence["pipelineRunId"] = run["id"]

    def deployment():
        _, items = api.call("GET", f"/deployments?applicationId={run['applicationId']}&limit=20", admin)
        for item in (items or {}).get("items", []):
            if item.get("pipelineRunId") == run["id"]:
                return item
        return None

    dep = wait_until("the deployment record (Jenkins build)", deployment, timeout=900, interval=3)
    evidence["deploymentId"] = dep["id"]

    victim = wait_until("a worker to run the playbook", lambda: playbook_parent(dep["id"]), timeout=300, interval=0.5)
    killed_at = time.monotonic()
    os.kill(victim, signal.SIGKILL)
    evidence["killedWorker"] = {"pid": victim, "at": datetime.now(timezone.utc).isoformat(), "signal": "SIGKILL"}
    print(f"killed worker {victim} while it ran the playbook for {dep['id']}")

    def terminal():
        current = deployment()
        return current if current and current["status"] in TERMINAL else None

    final = wait_until("the deployment to finish on the surviving worker", terminal, timeout=900, interval=2)
    recovered_seconds = round(time.monotonic() - killed_at, 1)
    survivors = worker_pids()
    evidence.update({
        "finalStatus": final["status"], "message": final.get("message"),
        "secondsFromKillToTerminal": recovered_seconds,
        "workersAfter": sorted(survivors), "survivorFinished": victim not in survivors and len(survivors) >= 1,
        "finishedAt": datetime.now(timezone.utc).isoformat(),
    })
    evidence["result"] = "passed" if final["status"] == "healthy" and evidence["survivorFinished"] else "failed"
    Path(args.out).write_text(json.dumps(evidence, indent=2) + "\n")
    print(f"{evidence['result']}: {final['status']} {recovered_seconds}s after the kill -> {args.out}")
    return 0 if evidence["result"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
