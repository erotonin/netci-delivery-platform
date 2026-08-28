#!/usr/bin/env python3
"""Jenkins CI loop gate: netCI starts a real build, and the build reports back.

This is the seam the rest of the platform is built on, and the one a unit test cannot
prove. The gate drives it end to end:

    netCI API -> router picks a controller -> pipeline job reconciled from the template
    -> build triggered -> ephemeral agent pod created in kind -> stages run
    -> SBOM/scan/signature published back to netCI -> policy verdict -> deployment

It also asserts the isolation claims of ADR-007 against the live cluster: the build ran
on a throwaway pod, not on the controller, and the pod is gone afterwards.

    python scripts/gate_jenkins_ci.py --api-url http://127.0.0.1:8100
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
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

BUILD_NAMESPACE = "netci-build"


def kubectl_json(recorder: EvidenceRecorder, name: str, *arguments: str) -> dict:
    result = recorder.run(
        name, ["kubectl", "--context", recorder.context["kubeContext"], *arguments, "-o", "json"]
    )
    return json.loads(result.stdout or "{}")


def watch_agent_pods(recorder: EvidenceRecorder, deadline: float) -> list[str]:
    """Record the ephemeral agent pods that exist while the build is running."""

    seen: dict[str, str] = {}
    while time.time() < deadline:
        pods = kubectl_json(
            recorder, "poll-agent-pods", "get", "pods", "-n", BUILD_NAMESPACE,
            "-l", "netci.io/agent=ephemeral",
        )
        for pod in pods.get("items", []):
            seen[pod["metadata"]["name"]] = pod["status"].get("phase", "Unknown")
        if seen:
            return sorted(seen)
        time.sleep(3)
    return sorted(seen)


def gate(recorder: EvidenceRecorder) -> None:
    require_tools(recorder, "kubectl", "docker")
    context = recorder.context
    client = NetciClient(context["apiUrl"], context["pipelineApiKey"])

    health = client.health()
    recorder.record("api-health", health)
    recorder.check_equal(
        "netCI is configured to drive Jenkins, not to wait for a hand-written callback",
        health.get("engines", {}).get("ci"),
        "jenkins",
    )

    controllers = {
        name: JenkinsClient(url, context["jenkinsUsername"], context["jenkinsToken"])
        for name, url in context["controllers"].items()
    }
    for name, jenkins in controllers.items():
        recorder.check(f"{name} is reachable", jenkins.reachable(), detail=jenkins.base_url)

    # The controller must not run builds itself -- that is the whole point of the
    # ephemeral agent design, and it is a live fact, not a config assertion.
    for name, jenkins in controllers.items():
        info = jenkins.get_json("/api/json?tree=numExecutors")
        recorder.check_equal(f"{name} has no executors of its own", info.get("numExecutors"), 0)
        clouds = jenkins.clouds_for_label("netci-ephemeral")
        recorder.check(
            f"{name} can provision the ephemeral agent label from a cloud",
            any("Kubernetes" in item for item in clouds),
            detail=clouds,
        )

    # A build that finished moments ago may still be tearing its pod down, so give the
    # namespace a moment to drain before asserting it is clean. Asserting instantly
    # would make the gate fail when run twice in a row, which is not a real defect.
    lingering: list[str] = []
    for _ in range(20):
        before = kubectl_json(recorder, "agent-pods-before", "get", "pods", "-n", BUILD_NAMESPACE)
        lingering = [item["metadata"]["name"] for item in before.get("items", [])]
        if not lingering:
            break
        time.sleep(3)
    recorder.check(
        "no build pods are lingering before the run", not lingering, detail=lingering
    )

    # ---------------------------------------------------------------- start a build
    name = f"gate-jenkins-{uuid.uuid4().hex[:6]}"
    application = client.create_application(
        {
            "name": name,
            "repositoryUrl": context["gitUrl"],
            "pipelineTemplate": "container-ci-cd-v1",
            "runtime": "docker",
        },
        idempotency_key=f"gate-app-{name}",
    )
    started = time.time()
    run = client.start_pipeline(
        application["id"],
        {
            "commitSha": context["commit"],
            "branch": context["branch"],
            "environment": "staging",
            "parameters": {"NETCI_APP_DIR": "sample-apps/hello-container"},
        },
        idempotency_key=f"gate-run-{name}",
    )
    recorder.record("pipeline-run", run)

    # netCI, not the gate, chose the controller and triggered the job.
    recorder.check(
        "netCI recorded a controller-qualified external run id",
        bool(run.get("jenkinsRunId")) and ":" in str(run.get("jenkinsRunId")),
        detail=run.get("jenkinsRunId"),
    )
    controller_id, _, external = str(run["jenkinsRunId"]).partition(":")
    recorder.check(
        "the chosen controller is one of the registered controllers",
        controller_id in controllers,
        detail={"chosen": controller_id, "registered": sorted(controllers)},
    )
    job_name, _, build_number = external.partition("#")
    recorder.check(
        "the queue item resolved to a real build number", build_number.isdigit(), detail=external
    )
    recorder.context["chosenController"] = controller_id
    recorder.context["jenkinsJob"] = job_name

    jenkins = controllers[controller_id]
    definition = jenkins.get_json(f"/job/{urllib.parse.quote(job_name)}/api/json?tree=_class,description")
    recorder.check(
        "netCI provisioned a pipeline job, not a freestyle project",
        "WorkflowJob" in str(definition.get("_class")),
        detail=definition.get("_class"),
    )

    # ------------------------------------------------------ the build runs on a pod
    pods = watch_agent_pods(recorder, deadline=time.time() + 180)
    recorder.check(
        "an ephemeral agent pod was created for the build", bool(pods), detail=pods
    )
    recorder.context["agentPods"] = pods
    if pods:
        pod = kubectl_json(recorder, "agent-pod-spec", "get", "pod", pods[0], "-n", BUILD_NAMESPACE)
        spec = pod["spec"]
        recorder.check_equal(
            "the agent pod does not auto-mount a Kubernetes API token",
            spec.get("automountServiceAccountToken"),
            False,
        )
        recorder.check(
            "the agent pod mounts no host path and no container socket",
            not any("hostPath" in volume for volume in spec.get("volumes") or []),
            detail=[volume.get("name") for volume in spec.get("volumes") or []],
        )
        recorder.check_equal(
            "the agent pod runs as a non-root user", spec["securityContext"].get("runAsNonRoot"), True
        )
        recorder.check(
            "the workspace is an emptyDir that dies with the pod",
            any("emptyDir" in volume for volume in spec.get("volumes") or []),
            detail=[sorted(volume) for volume in spec.get("volumes") or []],
        )

    # -------------------------------------------------------- wait for the callback
    # CI finishing does not make the *run* terminal: with NETCI_CD_MODE=none the run
    # stays `running` while netCI waits for a deployment result callback. The thing to
    # wait for is the artifact -- that is what CI was asked to produce.
    deadline = time.time() + context["timeout"]
    final: dict = {}
    while time.time() < deadline:
        final = client.pipeline(run["id"])
        if final.get("artifactDigest") or final["status"] in {"failed", "rolled_back", "cancelled"}:
            break
        time.sleep(5)
    duration = round(time.time() - started, 1)
    recorder.record("pipeline-final", {**final, "durationSeconds": duration})

    console = jenkins.console(job_name, int(build_number))
    recorder.record("jenkins-console", {"job": job_name, "build": build_number, "tail": console[-6000:]})

    logs = client.pipeline_logs(run["id"])["lines"]
    recorder.record("netci-pipeline-log", {"lines": logs})
    recorder.check(
        "netCI recorded that it dispatched the build to a controller",
        any(line.startswith(f"ci-dispatched controller={controller_id}") for line in logs),
        detail=logs[:10],
    )
    recorder.check(
        "the build called back to move the run out of queued",
        any("jenkins build" in line for line in logs),
        detail=logs,
    )

    if final.get("status") != "succeeded" and not final.get("artifactDigest"):
        raise GateFailure(
            f"the build did not produce an artifact: status={final.get('status')} "
            f"console tail: {console[-1500:]}"
        )

    recorder.check(
        "the build published an immutable digest through the callback",
        str(final.get("artifactDigest", "")).startswith("sha256:"),
        detail=final.get("artifactDigest"),
    )

    evidence = client.expect("GET", f"/pipeline-runs/{run['id']}/security-evidence")
    recorder.record("published-evidence", evidence)
    recorder.check_equal(
        "the evidence the build published describes the digest it reported",
        evidence.get("artifactDigest"),
        final.get("artifactDigest"),
    )
    recorder.check_equal("Syft generated the SBOM", evidence["sbom"]["generatedBy"], "syft")
    recorder.check_equal("Trivy produced the scan", evidence["vulnerabilityScan"]["scanner"], "trivy")
    recorder.check_equal("Cosign signed the artifact", evidence["signature"]["provider"], "cosign")
    recorder.check_equal("netCI allowed the artifact", evidence.get("decision"), "allow")

    recorder.check(
        "the run moved past CI rather than staying queued",
        final.get("status") in {"running", "waiting_approval", "succeeded"},
        detail=final.get("status"),
    )
    events = client.delivery_events(application["id"])
    recorder.record("delivery-events", events)
    recorder.check(
        "starting the run recorded the commit event DORA is measured from",
        any(item["eventType"] == "commit" and item["pipelineRunId"] == run["id"] for item in events["items"]),
        detail=[item["eventType"] for item in events["items"]],
    )

    # -------------------------------------------------- the pod is cleaned up after
    for _ in range(40):
        remaining = kubectl_json(
            recorder, "agent-pods-after", "get", "pods", "-n", BUILD_NAMESPACE,
            "-l", "netci.io/agent=ephemeral",
        ).get("items", [])
        if not remaining:
            break
        time.sleep(3)
    recorder.check(
        "the ephemeral agent pod was deleted once the build finished",
        not remaining,
        detail=[item["metadata"]["name"] for item in remaining],
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
    parser.add_argument("--kube-context", default=os.getenv("NETCI_KUBE_CONTEXT", "kind-netci-local"))
    parser.add_argument("--git-url", default=os.getenv("NETCI_GIT_URL", ""))
    parser.add_argument("--branch", default=os.getenv("NETCI_GIT_BRANCH", "main"))
    parser.add_argument("--commit", default=os.getenv("NETCI_GIT_COMMIT", ""))
    parser.add_argument("--timeout", type=float, default=float(os.getenv("NETCI_CI_TIMEOUT", "900")))
    arguments = parser.parse_args()

    controllers = controllers_from_env()
    if not controllers:
        print("set JENKINS_A_URL / JENKINS_B_URL (see scripts/jenkins_lab.sh up)", file=sys.stderr)
        return 2
    if not arguments.git_url:
        print("set NETCI_GIT_URL to a remote the build cluster can clone", file=sys.stderr)
        return 2

    recorder = EvidenceRecorder(
        "jenkins-ci-loop",
        description="netCI routes a run to a live Jenkins controller, the build executes on a "
        "throwaway agent pod, and the artifact plus its evidence come back through the API.",
    )
    recorder.context.update(
        {
            "apiUrl": arguments.api_url,
            "pipelineApiKey": arguments.pipeline_api_key,
            "kubeContext": arguments.kube_context,
            "controllers": controllers,
            "jenkinsUsername": os.getenv("JENKINS_A_USERNAME", "admin"),
            "jenkinsToken": os.getenv("JENKINS_A_API_TOKEN", "change-me-local-only"),
            "gitUrl": arguments.git_url,
            "branch": arguments.branch,
            "commit": arguments.commit or resolve_remote_commit(arguments.git_url, arguments.branch),
            "timeout": arguments.timeout,
        }
    )
    return run_gate(recorder, gate)


if __name__ == "__main__":
    raise SystemExit(main())
