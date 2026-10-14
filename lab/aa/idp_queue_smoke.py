#!/usr/bin/env python3
"""The IDP's own Jenkins adapter, through netci-queue, against the active-active replicas (ADR-082).

    PYTHONPATH=backend .venv/bin/python lab/aa/idp_queue_smoke.py

What it shows, with the IDP's code and nothing of the probes': the job the IDP generates is created
through the VIP (and so on both replicas); a build is submitted to netci-queue with every parameter
the IDP sends -- the per-run callback token as a password parameter the queue never shows back --
and followed to its end by run id; its log is read; a second build is cancelled through the queue.
Said plainly: the lab's replicas have no netCI shared library, so the generated pipeline fails when
it loads it. The build's result is FAILURE, read as "failed": the path is what is under test, not
the pipeline.
Evidence: lab/evidence/aa-idp-queue-<timestamp>.json.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import time
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(Path(__file__).parent))
from app.adapters.ci_launcher import CiLaunchRequest  # noqa: E402
from app.adapters.jenkins_http import JenkinsHttpConfig  # noqa: E402
from app.adapters.jenkins_queue import JenkinsQueueAdapter, RunQueueConfig  # noqa: E402

import phase3_probe as p3  # noqa: E402

Q = ROOT / ".netci-gate" / "aa" / "queue"


def request(app) -> CiLaunchRequest:
    return CiLaunchRequest(
        application_id=app, application_name="payments-api", repository_url="https://git.example/payments/payments-api.git",
        pipeline_template="container-ci-cd-v1", runtime="docker", stages=("checkout", "build"), pipeline_run_id=uuid4(),
        commit_sha="a" * 40, branch="main", environment="dev", correlation_id="smoke", parameters={},
    )


def until_terminal(adapter, run_id, timeout=300):
    end, seen = time.time() + timeout, []
    while time.time() < end:
        st = adapter.get_status(run_id).status
        if not seen or seen[-1] != st:
            seen.append(st)
        if st in ("succeeded", "failed", "cancelled", "unknown"):
            break
        time.sleep(2)
    return seen


def main() -> int:
    started = dt.datetime.now(dt.timezone.utc)
    adapter = JenkinsQueueAdapter(
        JenkinsHttpConfig(base_url=p3.VIP, username=(Q / "jenkins-user").read_text().strip(),
                          api_token=(Q / "jenkins-token").read_text().strip(), callback_url="http://netci.lab.invalid"),
        RunQueueConfig(url="http://192.168.122.212:30091", token=(Q / "client-token").read_text().strip()),
    )
    app = uuid4()
    job = adapter.create_or_update_job(app, "container-ci-cd-v1")
    on_both = p3.until(lambda: all(p3.R(r).job(job) is not None for r in ("r0", "r1")), 30, 0.5)
    facts = {"scenario": "aa-idp-queue", "started": started.isoformat(timespec="seconds"), "job": job,
             "jobOnBothReplicasAfterSeconds": None if on_both is None else round(on_both, 2)}

    req = request(app)
    run = adapter.trigger_ci_run(job, req, "smoke-callback-token-" + uuid4().hex)
    again = adapter.trigger_ci_run(job, req, "a-new-token-on-retry")
    facts["build"] = {"runId": run.run_id, "retriedIsSameRun": again.run_id == run.run_id,
                      "statuses": until_terminal(adapter, run.run_id)}
    logs = adapter.get_logs(run.run_id)
    facts["build"]["logLines"] = len(logs)
    facts["build"]["logLoadsTheLibrary"] = any("netci" in line.lower() and "library" in line.lower() for line in logs)
    view = adapter._view(run.run_id.split(":", 1)[1])
    facts["build"]["queueShowsTheToken"] = "smoke-callback-token" in json.dumps(view)
    facts["build"]["queueView"] = {k: view.get(k) for k in ("state", "result", "buildNumber", "buildUrl")}

    second = adapter.trigger_ci_run(job, request(app), "tok-" + uuid4().hex)
    adapter.abort(second.run_id)
    facts["cancelled"] = {"runId": second.run_id, "statuses": until_terminal(adapter, second.run_id)}
    print(json.dumps(facts, indent=1))
    out = ROOT / "lab" / "evidence" / f"aa-idp-queue-{started.strftime('%Y%m%dT%H%M%SZ')}.json"
    out.write_text(json.dumps(facts, indent=2) + "\n")
    print("evidence:", out.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
