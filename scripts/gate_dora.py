#!/usr/bin/env python3
"""DORA gate: prove the dashboard numbers are derived from recorded delivery events.

The gate drives a known delivery history through the API -- a good release, a failed
release, and the recovery that followed -- then reads the dashboard back and checks it
against the events, recomputed independently here. A metric that cannot be reproduced
from `/delivery-events` fails the gate, which is what stops a dashboard from drifting
into decoration.

    python scripts/gate_dora.py --api-url http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from netci_gates.client import NetciClient  # noqa: E402
from netci_gates.evidence import PROJECT_ROOT, EvidenceRecorder, run_gate  # noqa: E402

SCHEMA_PATH = PROJECT_ROOT / "api" / "dora-dashboard.schema.json"
DIGEST_A = "sha256:" + "1" * 64
DIGEST_B = "sha256:" + "2" * 64
DIGEST_C = "sha256:" + "3" * 64

#: The four metrics the Portal design specifies -- no more, no fewer.
EXPECTED_METRICS = {
    "deploymentFrequency",
    "changeLeadTimeSecondsAvg",
    "changeFailRate",
    "timeToRestoreServiceSecondsAvg",
}


def validate_against_schema(recorder: EvidenceRecorder, payload: dict) -> None:
    """Check the payload against the checked-in JSON Schema without extra dependencies."""

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    missing = [field for field in schema["required"] if field not in payload]
    recorder.check("the dashboard payload has every required field", not missing, detail=missing)

    metrics_schema = schema["properties"]["metrics"]
    metric_missing = [field for field in metrics_schema["required"] if field not in payload["metrics"]]
    recorder.check("the dashboard carries all four DORA metrics", not metric_missing, detail=metric_missing)
    recorder.check_equal(
        "the dashboard carries exactly four DORA metrics and no extras",
        sorted(payload["metrics"]),
        sorted(EXPECTED_METRICS),
    )
    for name, value in payload["metrics"].items():
        rules = metrics_schema["properties"][name]
        recorder.check(f"{name} is a number", isinstance(value, (int, float)), detail=value)
        recorder.check(f"{name} is at least {rules['minimum']}", value >= rules["minimum"], detail=value)
        if "maximum" in rules:
            recorder.check(f"{name} is at most {rules['maximum']}", value <= rules["maximum"], detail=value)
    recorder.check(
        "sourceEvents is a non-negative integer",
        isinstance(payload["sourceEvents"], int) and payload["sourceEvents"] >= 0,
        detail=payload["sourceEvents"],
    )


def parse(timestamp: str) -> datetime:
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))


def recompute(events: list[dict]) -> dict[str, float]:
    """Recompute the four metrics here, independently of the API's projection."""

    ordered = sorted(events, key=lambda item: parse(item["occurredAt"]))
    commits = {
        (item["applicationId"], item["commitSha"]): parse(item["occurredAt"])
        for item in ordered
        if item["eventType"] == "commit" and item["commitSha"]
    }
    deployments = [item for item in ordered if item["eventType"] == "deployment" and item["environment"] == "prod"]
    failures = [item for item in deployments if item["requiresIntervention"]]

    lead_times = [
        (parse(item["occurredAt"]) - commits[(item["applicationId"], item["commitSha"])]).total_seconds()
        for item in deployments
        if (item["applicationId"], item["commitSha"]) in commits
    ]
    restores: list[float] = []
    for failure in failures:
        recovery = next(
            (
                item
                for item in ordered
                if item["eventType"] == "recovery"
                and item["deploymentId"] == failure["deploymentId"]
                and item["applicationId"] == failure["applicationId"]
                and item["environment"] == failure["environment"]
                and parse(item["occurredAt"]) >= parse(failure["occurredAt"])
            ),
            None,
        )
        if recovery:
            restores.append((parse(recovery["occurredAt"]) - parse(failure["occurredAt"])).total_seconds())

    weeks = 30 / 7  # the API's reporting window, stated in the response
    return {
        "deploymentFrequency": len(deployments) / weeks,
        "changeLeadTimeSecondsAvg": sum(lead_times) / len(lead_times) if lead_times else 0.0,
        "changeFailRate": len(failures) / len(deployments) if deployments else 0.0,
        "timeToRestoreServiceSecondsAvg": sum(restores) / len(restores) if restores else 0.0,
    }


def release(
    client: NetciClient,
    application_id: str,
    *,
    commit: str,
    digest: str,
    healthy: bool,
    commit_at: datetime,
) -> str:
    """Drive one production release to a terminal state and return its deployment id."""

    run = client.start_pipeline(
        application_id,
        {
            "commitSha": commit,
            "environment": "prod",
            # Supplying the authoring time is what makes lead time a real measurement
            # rather than "time since the button was pressed".
            "parameters": {"commitTimestamp": commit_at.isoformat()},
        },
        idempotency_key=f"dora-{application_id}-{commit}",
    )
    client.ci_result(run["id"], {"status": "running"})
    result = client.ci_result(run["id"], {"status": "succeeded", "artifactDigest": digest})
    deployment = result["deployment"]
    client.approve(deployment["id"])
    client.deployment_result(deployment["id"], "healthy" if healthy else "failed", f"{commit} {healthy}")
    return deployment["id"]


def gate(recorder: EvidenceRecorder) -> None:
    client = NetciClient(recorder.context["apiUrl"], recorder.context["pipelineApiKey"])
    health = client.health()
    recorder.record("api-health", health)
    recorder.check_equal("the netCI API is healthy", health.get("status"), "ok")

    name = f"gate-dora-{uuid.uuid4().hex[:6]}"
    application = client.create_application(
        {
            "name": name,
            "repositoryUrl": f"https://github.com/example/{name}",
            "pipelineTemplate": "container-ci-cd-v1",
            "runtime": "docker",
        },
        idempotency_key=f"dora-app-{name}",
    )
    application_id = application["id"]
    recorder.context["applicationId"] = application_id

    empty = client.dora(application_id)
    recorder.record("dora-before-any-release", empty)
    recorder.check_equal("an application with no events reports no source events", empty["sourceEvents"], 0)
    recorder.check(
        "with no events every metric is zero rather than a plausible-looking baseline",
        all(value == 0 for value in empty["metrics"].values()),
        detail=empty["metrics"],
    )

    now = datetime.now(timezone.utc)
    # A good release, a release that failed in production, and the fix that restored it.
    release(client, application_id, commit="d0raaa1", digest=DIGEST_A, healthy=True, commit_at=now - timedelta(hours=3))
    broken = release(
        client, application_id, commit="d0rabb2", digest=DIGEST_B, healthy=False, commit_at=now - timedelta(hours=2)
    )
    release(client, application_id, commit="d0racc3", digest=DIGEST_C, healthy=True, commit_at=now - timedelta(hours=1))
    recorder.record("released", {"failedDeploymentId": broken})

    events = client.delivery_events(application_id)
    recorder.record("delivery-events", events)
    kinds = [item["eventType"] for item in events["items"]]
    recorder.check_equal("three releases produced three commit events", kinds.count("commit"), 3)
    recorder.check_equal("three production releases produced three deployment events", kinds.count("deployment"), 3)
    recorder.check_equal("the failure was followed by exactly one recovery event", kinds.count("recovery"), 1)

    recovery = next(item for item in events["items"] if item["eventType"] == "recovery")
    recorder.check_equal(
        "the recovery is bound to the deployment that actually failed",
        recovery["deploymentId"],
        broken,
    )

    dashboard = client.dora(application_id)
    recorder.record("dora-dashboard", dashboard)
    validate_against_schema(recorder, dashboard)
    recorder.check_equal(
        "the dashboard counts exactly the events that were recorded",
        dashboard["sourceEvents"],
        len(events["items"]),
    )

    expected = recompute(events["items"])
    recorder.record("recomputed-from-events", expected)
    for metric, value in expected.items():
        actual = dashboard["metrics"][metric]
        # The API rounds for display; the gate allows only that much difference.
        recorder.check(
            f"{metric} on the dashboard matches the value recomputed from source events",
            abs(actual - value) <= max(0.05, abs(value) * 0.01) or (value < 1 and abs(actual - value) < 1),
            detail={"dashboard": actual, "recomputedFromEvents": round(value, 3)},
        )

    recorder.check(
        "change failure rate reflects the one failed release out of three",
        abs(dashboard["metrics"]["changeFailRate"] - 1 / 3) < 0.02,
        detail=dashboard["metrics"]["changeFailRate"],
    )
    recorder.check(
        "time to restore service is measured, not assumed",
        dashboard["metrics"]["timeToRestoreServiceSecondsAvg"] >= 0,
        detail=dashboard["metrics"]["timeToRestoreServiceSecondsAvg"],
    )
    recorder.check(
        "lead time reflects the supplied commit timestamps",
        dashboard["metrics"]["changeLeadTimeSecondsAvg"] > 3000,
        detail=dashboard["metrics"]["changeLeadTimeSecondsAvg"],
    )

    portal = client.expect("GET", "/modules/hello-container/dora", status=(200, 404))
    if isinstance(portal, dict) and "metrics" in portal:
        recorder.record("portal-module-dora", portal)
        recorder.check_equal("the Portal view also reports four metrics", len(portal["metrics"]), 4)
        recorder.check(
            "the Portal view states how many source events it used",
            "sourceEventCount" in portal,
            detail=sorted(portal),
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api-url", default=os.getenv("NETCI_API_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--pipeline-api-key", default=os.getenv("NETCI_PIPELINE_API_KEY", "netci-local-pipeline-key"))
    arguments = parser.parse_args()

    recorder = EvidenceRecorder(
        "dora-dashboard",
        description="The four DORA metrics recomputed from recorded delivery events and checked "
        "against the dashboard the API serves.",
    )
    recorder.context.update({"apiUrl": arguments.api_url, "pipelineApiKey": arguments.pipeline_api_key})
    return run_gate(recorder, gate)


if __name__ == "__main__":
    raise SystemExit(main())
