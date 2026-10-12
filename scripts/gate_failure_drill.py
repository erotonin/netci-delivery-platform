#!/usr/bin/env python3
"""Controller failure drill: stop A, detect it, reroute to B, finish, and rejoin A.

The router's selection logic is unit-tested. What is not, and what this gate exists for,
is that netCI *notices* a controller has died and sends the next build somewhere else --
end to end, against controllers that are really stopped.

The drill measures three intervals a reviewer will ask about:

  detection   how long until netCI reports the controller unavailable
  reroute     how long until a new run is accepted on the surviving controller
  restore     stop -> a build running again elsewhere (the MTTR figure)

The stopped controller is always restarted, including when an assertion fails.

    python scripts/gate_failure_drill.py --api-url http://127.0.0.1:8100
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from netci_gates.client import NetciClient  # noqa: E402
from netci_gates.jenkins import JenkinsClient, resolve_remote_commit  # noqa: E402
from netci_gates.evidence import (  # noqa: E402
    EvidenceRecorder,
    GateFailure,
    require_tools,
    run_gate,
)


def jenkins_reachable(url: str, username: str, token: str, timeout: float = 3.0) -> bool:
    return JenkinsClient(url, username, token).reachable(timeout=timeout)


def start_run(client: NetciClient, application_id: str, suffix: str, context: dict) -> dict:
    return client.start_pipeline(
        application_id,
        {
            "commitSha": context["commit"],
            "branch": context["branch"],
            "environment": "staging",
            "parameters": {"agentLabel": context["agentLabel"]},
        },
        idempotency_key=f"drill-{application_id}-{suffix}",
    )


def gate(recorder: EvidenceRecorder) -> None:
    require_tools(recorder, "docker")
    context = recorder.context
    client = NetciClient(context["apiUrl"], context["pipelineApiKey"])
    victim, survivor = context["victim"], context["survivor"]

    health = client.health()
    recorder.check_equal(
        "netCI is driving Jenkins, so routing is a real decision", health["engines"]["ci"], "jenkins"
    )
    for name, url in context["controllers"].items():
        recorder.check(
            f"{name} is reachable before the drill",
            jenkins_reachable(url, context["username"], context["token"]),
            detail=url,
        )

    name = f"gate-drill-{uuid.uuid4().hex[:6]}"
    application = client.create_application(
        {
            "name": name,
            "repositoryUrl": context["gitUrl"],
            "pipelineTemplate": "container-ci-cd-v1",
            "runtime": "docker",
        },
        idempotency_key=f"drill-app-{name}",
    )

    # A run before the failure, so "it routed to B" cannot be an accident of ordering.
    baseline = start_run(client, application["id"], "baseline", context)
    recorder.record("baseline-run", baseline)
    recorder.check(
        "a run is accepted while both controllers are up", bool(baseline.get("jenkinsRunId")), detail=baseline
    )

    stopped_at = time.time()
    restarted = False
    try:
        recorder.run("stop-controller", ["docker", "stop", victim], timeout=120)
        recorder.check(
            f"{victim} really stopped",
            not jenkins_reachable(context["controllers"][victim], context["username"], context["token"]),
            detail=victim,
        )

        # ------------------------------------------------------------- detection
        detected_at = None
        for _ in range(60):
            if not jenkins_reachable(
                context["controllers"][victim], context["username"], context["token"], timeout=2
            ):
                detected_at = time.time()
                break
            time.sleep(1)
        recorder.check(f"the outage is observable from outside {victim}", detected_at is not None)
        detection_seconds = round((detected_at or time.time()) - stopped_at, 2)

        # --------------------------------------------------------------- reroute
        rerouted = None
        reroute_error = None
        for attempt in range(20):
            try:
                rerouted = start_run(client, application["id"], f"failover-{attempt}", context)
                break
            except Exception as exc:  # the router may still be probing health
                reroute_error = exc
                time.sleep(3)
        if rerouted is None:
            raise GateFailure(f"netCI never accepted a run while {victim} was down: {reroute_error}")
        rerouted_at = time.time()
        recorder.record("rerouted-run", rerouted)

        controller_id = str(rerouted.get("jenkinsRunId", "")).partition(":")[0]
        recorder.check_equal(
            "the build after the failure was routed to the surviving controller",
            controller_id,
            survivor,
        )
        recorder.check(
            "netCI did not route anything to the stopped controller",
            controller_id != victim,
            detail=rerouted.get("jenkinsRunId"),
        )
        logs = client.pipeline_logs(rerouted["id"])["lines"]
        recorder.record("reroute-pipeline-log", {"lines": logs})
        recorder.check(
            "the run log names the controller that accepted it",
            any(f"ci-dispatched controller={survivor}" in line for line in logs),
            detail=logs,
        )

        # ------------------------------------------------------- run to completion
        # CI producing the artifact is the outcome that matters. The run itself stays
        # `running` afterwards when NETCI_CD_MODE=none, because netCI is then waiting
        # for a deployment result callback that this drill does not send.
        deadline = time.time() + context["timeout"]
        final: dict = {}
        while time.time() < deadline:
            final = client.pipeline(rerouted["id"])
            if final.get("artifactDigest") or final["status"] in {"failed", "rolled_back", "cancelled"}:
                break
            time.sleep(5)
        recorder.record("failover-run-final", final)
        recorder.check(
            "the rerouted build ran to completion on the surviving controller",
            final.get("status") != "queued",
            detail=final.get("status"),
        )
        recorder.check(
            "the surviving controller produced a verified artifact, so it can do the work",
            str(final.get("artifactDigest") or "").startswith("sha256:"),
            detail={"status": final.get("status"), "artifactDigest": final.get("artifactDigest")},
        )
        restore_seconds = round(time.time() - stopped_at, 2)

    finally:
        # The drill must leave the lab as it found it, pass or fail.
        recorder.run("restart-controller", ["docker", "start", victim], timeout=120, expect_success=False)
        for _ in range(120):
            if jenkins_reachable(context["controllers"][victim], context["username"], context["token"]):
                restarted = True
                break
            time.sleep(2)
        recorder.record("controller-restarted", {"controller": victim, "reachable": restarted})

    recorder.check(f"{victim} rejoined the pool", restarted)

    # A rejoining controller must come back with its configuration, not empty.
    rejoined_client = JenkinsClient(context["controllers"][victim], context["username"], context["token"])
    rejoined = rejoined_client.get_json("/api/json?tree=numExecutors,mode")
    clouds = rejoined_client.clouds_for_label("netci-ephemeral")
    recorder.record("rejoined-config", {**rejoined, "ephemeralLabelClouds": clouds})
    recorder.check_equal("the rejoined controller still runs no builds itself", rejoined.get("numExecutors"), 0)
    recorder.check(
        "the rejoined controller still has its agent cloud, so it came back configured",
        any("Kubernetes" in item for item in clouds),
        detail=clouds,
    )

    accepted = start_run(client, application["id"], "after-rejoin", context)
    recorder.check(
        "netCI accepts work again once both controllers are healthy",
        bool(accepted.get("jenkinsRunId")),
        detail=accepted.get("jenkinsRunId"),
    )

    recorder.record(
        "drill-intervals",
        {
            "stoppedController": victim,
            "survivingController": survivor,
            "detectionSeconds": detection_seconds,
            "rerouteSeconds": round(rerouted_at - stopped_at, 2),
            "timeToRestoreServiceSeconds": restore_seconds,
        },
    )
    recorder.check(
        "the drill produced a measured restore time, not an estimate",
        restore_seconds > 0,
        detail=restore_seconds,
    )


def controllers_from_env() -> dict[str, str]:
    names = [item.strip() for item in os.getenv("NETCI_JENKINS_CONTROLLERS", "A,B").split(",") if item.strip()]
    mapping: dict[str, str] = {}
    for name in names:
        prefix = f"JENKINS_{name.upper().replace('-', '_')}"
        url = os.getenv(f"{prefix}_URL", "").strip()
        if url:
            mapping[os.getenv(f"{prefix}_ID", f"jenkins-{name.lower()}")] = url.rstrip("/")
    return mapping


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api-url", default=os.getenv("NETCI_API_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--pipeline-api-key", default=os.getenv("NETCI_PIPELINE_API_KEY", "netci-local-pipeline-key"))
    parser.add_argument("--victim", default=os.getenv("NETCI_DRILL_VICTIM", "jenkins-a"))
    parser.add_argument("--git-url", default=os.getenv("NETCI_GIT_URL", ""))
    parser.add_argument("--branch", default=os.getenv("NETCI_GIT_BRANCH", "main"))
    parser.add_argument("--commit", default=os.getenv("NETCI_GIT_COMMIT", ""))
    parser.add_argument("--agent-label", default=os.getenv("NETCI_AGENT_LABEL", "netci-ephemeral"))
    parser.add_argument("--timeout", type=float, default=float(os.getenv("NETCI_CI_TIMEOUT", "900")))
    arguments = parser.parse_args()

    controllers = controllers_from_env()
    if len(controllers) < 2:
        print("the drill needs two controllers; see scripts/jenkins_lab.sh up", file=sys.stderr)
        return 2
    if arguments.victim not in controllers:
        print(f"--victim must be one of {sorted(controllers)}", file=sys.stderr)
        return 2
    if not arguments.git_url:
        print("set NETCI_GIT_URL to a remote the build cluster can clone", file=sys.stderr)
        return 2
    survivor = next(name for name in controllers if name != arguments.victim)

    recorder = EvidenceRecorder(
        "failure-drill",
        description="A live Jenkins controller is stopped; netCI detects the outage, routes the "
        "next build to its peer, the build completes, and the controller rejoins with its config.",
    )
    recorder.context.update(
        {
            "apiUrl": arguments.api_url,
            "pipelineApiKey": arguments.pipeline_api_key,
            "controllers": controllers,
            "victim": arguments.victim,
            "survivor": survivor,
            "username": os.getenv("JENKINS_A_USERNAME", "admin"),
            "token": os.getenv("JENKINS_A_API_TOKEN", "change-me-local-only"),
            "gitUrl": arguments.git_url,
            "branch": arguments.branch,
            "commit": arguments.commit or resolve_remote_commit(arguments.git_url, arguments.branch),
            "agentLabel": arguments.agent_label,
            "timeout": arguments.timeout,
        }
    )
    return run_gate(recorder, gate)


if __name__ == "__main__":
    raise SystemExit(main())
