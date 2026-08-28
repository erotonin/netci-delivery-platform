#!/usr/bin/env python3
"""Benchmark gate: what the ephemeral agent actually costs, measured against a shared one.

ADR-007 chooses isolation over speed. That is only an engineering decision if the price
is known, so this gate runs the *same* pipeline on a long-lived shared agent and on a
throwaway pod, several times each, and reports the difference from Jenkins' own timings.

The expected shape of the result is the interesting part: the ephemeral agent should be
slower to start (it provisions a pod per build) and should never report a cache hit,
because there is no previous build's workspace to hit. A cache hit on the ephemeral
agent would mean the isolation is not real, so the gate fails on it.

    python scripts/gate_benchmark.py --runs 3
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from netci_gates.evidence import (  # noqa: E402
    PROJECT_ROOT,
    EvidenceRecorder,
    GateFailure,
    require_tools,
    run_gate,
)
from netci_gates.jenkins import JenkinsClient  # noqa: E402

sys.path.insert(0, str(PROJECT_ROOT / "backend"))

# The same parameter list netCI's own managed jobs are created with. Importing it
# rather than restating it is what keeps the benchmark measuring the real pipeline:
# a job missing NETCI_BASE_IMAGE builds from an unqualified short name and fails,
# and a job that drifts from the adapter's list stops being a comparable workload.
from app.adapters.jenkins_http import JOB_PARAMETERS  # noqa: E402

BENCH_JOB = "netci-benchmark"


def job_config_xml() -> bytes:
    """A pipeline job that takes the agent label as a parameter.

    Both modes run the identical definition; only the label differs. Comparing two
    different job definitions would measure the definitions, not the agents.
    """

    parameters = "".join(
        "<hudson.model.StringParameterDefinition>"
        f"<name>{name}</name><defaultValue></defaultValue><trim>true</trim>"
        "</hudson.model.StringParameterDefinition>"
        for name in JOB_PARAMETERS
    )
    script = (
        "@Library('netci-shared-library') _\n"
        "netciPipeline(template: params.NETCI_TEMPLATE ?: 'container-ci-cd-v1')\n"
    )
    return (
        "<?xml version='1.1' encoding='UTF-8'?>\n"
        "<flow-definition plugin=\"workflow-job\">\n"
        "  <description>netCI benchmark job. Same definition on both agent types.</description>\n"
        "  <keepDependencies>false</keepDependencies>\n"
        "  <properties><hudson.model.ParametersDefinitionProperty>"
        f"<parameterDefinitions>{parameters}</parameterDefinitions>"
        "</hudson.model.ParametersDefinitionProperty></properties>\n"
        "  <definition class=\"org.jenkinsci.plugins.workflow.cps.CpsFlowDefinition\" plugin=\"workflow-cps\">\n"
        f"    <script>{script}</script>\n"
        "    <sandbox>true</sandbox>\n"
        "  </definition>\n"
        "  <disabled>false</disabled>\n"
        "</flow-definition>\n"
    ).encode()


def ensure_job(recorder: EvidenceRecorder, jenkins: JenkinsClient) -> None:
    """Create the job, or update it in place if a previous run left one behind.

    Jenkins answers `createItem` for an existing name with 400, not 409, so both are
    treated as "it is already there" and the config is pushed over the top. That keeps
    repeated runs measuring the same definition instead of a stale one.
    """

    config = job_config_xml()
    status, _, body = jenkins.post(
        f"/createItem?name={BENCH_JOB}&mode=org.jenkinsci.plugins.workflow.job.WorkflowJob",
        body=config,
        content_type="application/xml",
    )
    if status in {400, 409}:
        status, _, body = jenkins.post(
            f"/job/{BENCH_JOB}/config.xml", body=config, content_type="application/xml"
        )
    recorder.record("ensure-benchmark-job", {"job": BENCH_JOB, "status": status})
    if not 200 <= status < 300:
        raise GateFailure(
            f"could not provision {BENCH_JOB}: HTTP {status} {body[:400].decode(errors='replace')}"
        )


def gate(recorder: EvidenceRecorder) -> None:
    require_tools(recorder, "kubectl")
    context = recorder.context
    # The shared client, not a local copy: it carries the cookie jar the CSRF crumb is
    # bound to, without which every POST below comes back 403.
    jenkins = JenkinsClient(context["url"], context["username"], context["token"])

    labels = jenkins.online_labels()
    recorder.record("online-agent-labels", {"labels": sorted(labels)})
    recorder.check(
        "a shared agent is online to serve as the baseline",
        "netci-shared" in labels,
        detail=sorted(labels),
    )

    ensure_job(recorder, jenkins)

    trigger = [
        str(PROJECT_ROOT / ".venv" / "bin" / "python"),
        str(PROJECT_ROOT / "scripts" / "jenkins_build.py"),
        "--url", context["url"],
        "--username", context["username"],
        "--token", context["token"],
        "--job", BENCH_JOB,
        "--git-url", context["gitUrl"],
        "--branch", context["branch"],
    ]
    report = PROJECT_ROOT / "evidence" / "benchmarks" / "report.json"
    recorder.run(
        "run-benchmark",
        [
            str(PROJECT_ROOT / ".venv" / "bin" / "python"),
            str(PROJECT_ROOT / "scripts" / "benchmark.py"),
            "--runs", str(context["runs"]),
            "--application-id", context["applicationId"],
            "--environment", context["environment"],
            "--report", str(report),
            "--csv", str(PROJECT_ROOT / "evidence" / "benchmarks" / "samples.csv"),
            # The threshold exists to catch an unexpected regression, not to re-litigate
            # ADR-007. An ephemeral agent provisions a pod per build, so it is expected
            # to be several times slower to start than an always-on agent; only a delta
            # beyond this is a finding.
            "--regression-threshold", str(context["threshold"]),
            "--baseline-command", " ".join(trigger + ["--label", "netci-shared"]),
            "--ephemeral-command", " ".join(trigger + ["--label", "netci-ephemeral"]),
        ],
        timeout=context["runs"] * 1800,
        expect_success=False,
    )

    payload = json.loads(report.read_text(encoding="utf-8"))
    recorder.record("benchmark-report", payload)
    recorder.check_equal(
        "the report was produced for the environment it was measured in",
        payload.get("environment"),
        context["environment"],
    )

    schema = json.loads((PROJECT_ROOT / "api" / "benchmark-report.schema.json").read_text(encoding="utf-8"))
    missing = [field for field in schema["required"] if field not in payload]
    recorder.check("the report has every required field", not missing, detail=missing)
    for mode in ("baseline", "ephemeral"):
        mode_missing = [field for field in schema["$defs"]["mode"]["required"] if field not in payload[mode]]
        recorder.check(f"the {mode} section is complete", not mode_missing, detail=mode_missing)
        recorder.check_equal(f"the {mode} mode ran every requested sample", payload[mode]["runs"], context["runs"])
        recorder.check_equal(f"every {mode} build succeeded", payload[mode]["successRate"], 1.0)

    recorder.check(
        "the comparison reached a conclusion rather than an inconclusive result",
        payload["comparison"]["conclusion"] in {"acceptable", "regression"},
        detail=payload["comparison"],
    )
    recorder.check(
        "phase timings were measured, not defaulted to zero",
        payload["ephemeral"]["totalSecondsAvg"] > 0 and payload["baseline"]["totalSecondsAvg"] > 0,
        detail={
            "baselineTotal": payload["baseline"]["totalSecondsAvg"],
            "ephemeralTotal": payload["ephemeral"]["totalSecondsAvg"],
        },
    )

    # The isolation claim, expressed as a measurement.
    recorder.check(
        "the ephemeral agent pays provisioning that the shared agent does not",
        payload["ephemeral"]["provisioningSecondsAvg"] > payload["baseline"]["provisioningSecondsAvg"],
        detail={
            "ephemeral": payload["ephemeral"]["provisioningSecondsAvg"],
            "baseline": payload["baseline"]["provisioningSecondsAvg"],
        },
    )
    recorder.check(
        "the ephemeral agent never reuses a previous build's cache",
        payload["comparison"]["cacheHitRate"] == 0,
        detail=payload["comparison"]["cacheHitRate"],
    )
    recorder.check(
        "the ephemeral agent starts from an empty workspace, so it pays a full checkout",
        payload["ephemeral"]["checkoutSecondsAvg"] > payload["baseline"]["checkoutSecondsAvg"],
        detail={
            "ephemeral": payload["ephemeral"]["checkoutSecondsAvg"],
            "baseline": payload["baseline"]["checkoutSecondsAvg"],
        },
    )
    # A stage the classifier does not recognise used to be counted as cleanup, which
    # reported an 18-second checkout as teardown and aimed the conclusion at the wrong
    # cost. Unattributed time is now visible, and a large amount of it is a defect.
    unattributed = max(payload["baseline"]["otherSecondsAvg"], payload["ephemeral"]["otherSecondsAvg"])
    recorder.check(
        "every measured second is attributed to a named phase",
        unattributed < 1.0,
        detail={
            "baselineOtherSecondsAvg": payload["baseline"]["otherSecondsAvg"],
            "ephemeralOtherSecondsAvg": payload["ephemeral"]["otherSecondsAvg"],
        },
    )

    # Where the difference actually is, phase by phase. The total on its own says the
    # ephemeral agent is slower; only the breakdown says what to do about it.
    def cost(phase: str) -> float:
        return round(payload["ephemeral"][phase] - payload["baseline"][phase], 3)

    breakdown = {
        phase: cost(f"{phase}SecondsAvg")
        for phase in ("queue", "provisioning", "checkout", "cacheRestore", "build", "cleanup")
    }
    recorder.record(
        "conclusion",
        {
            "baselineTotalSecondsAvg": payload["baseline"]["totalSecondsAvg"],
            "ephemeralTotalSecondsAvg": payload["ephemeral"]["totalSecondsAvg"],
            "totalDeltaPercent": payload["comparison"]["totalDeltaPercent"],
            "verdict": payload["comparison"]["conclusion"],
            "extraSecondsByPhase": breakdown,
            "dominantPhase": max(breakdown, key=lambda phase: breakdown[phase]),
            "reading": "the delta is the price of per-build isolation, which ADR-007 accepts "
            "deliberately. The breakdown says which part of it is inherent -- provisioning a "
            "pod and cloning into an empty workspace -- and which is worth attacking.",
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=os.getenv("JENKINS_A_URL", ""))
    parser.add_argument("--username", default=os.getenv("JENKINS_A_USERNAME", "admin"))
    parser.add_argument("--token", default=os.getenv("JENKINS_A_API_TOKEN", "change-me-local-only"))
    parser.add_argument("--runs", type=int, default=int(os.getenv("NETCI_BENCHMARK_RUNS", "3")))
    parser.add_argument("--git-url", default=os.getenv("NETCI_GIT_URL", ""))
    parser.add_argument("--branch", default=os.getenv("NETCI_GIT_BRANCH", "main"))
    parser.add_argument("--application-id", default=os.getenv("NETCI_BENCHMARK_APPLICATION", "netci-benchmark"))
    parser.add_argument(
        "--regression-threshold",
        type=float,
        default=float(os.getenv("NETCI_BENCHMARK_THRESHOLD", "400")),
        help="percent slower than the shared agent that still counts as acceptable",
    )
    arguments = parser.parse_args()

    if not arguments.url:
        print("set --url or JENKINS_A_URL (see scripts/jenkins_lab.sh up)", file=sys.stderr)
        return 2
    if not arguments.git_url:
        print("set NETCI_GIT_URL to a remote the agents can clone", file=sys.stderr)
        return 2

    recorder = EvidenceRecorder(
        "benchmark",
        description="The same pipeline run repeatedly on a shared agent and on ephemeral pods, "
        "with Jenkins' own phase timings compared and the isolation cost quantified.",
    )
    recorder.context.update(
        {
            "url": arguments.url.rstrip("/"),
            "username": arguments.username,
            "token": arguments.token,
            "runs": arguments.runs,
            "gitUrl": arguments.git_url,
            "branch": arguments.branch,
            "applicationId": arguments.application_id,
            "threshold": arguments.regression_threshold,
            "environment": os.getenv("NETCI_EVIDENCE_ENV", "ubuntu-24.04-kind-local"),
        }
    )
    return run_gate(recorder, gate)


if __name__ == "__main__":
    raise SystemExit(main())
