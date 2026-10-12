#!/usr/bin/env python3
"""Backstage gate: a second portal creates an application through the same netCI API.

ADR-002 claims Backstage is another *client* of netCI, not a second business core. The
way to test that claim is to let a real Backstage instance run the checked-in Software
Template and then check netCI's own state: the application must exist, with the runtime
and template the form asked for, created through the documented proxy rather than by a
side channel.

    python scripts/gate_backstage.py --backstage-url http://127.0.0.1:7007

Bring the instance up first with `bash scripts/backstage_lab.sh up`, which scaffolds an
app, wires the proxy fragment from backstage/app-config.example.yaml and registers
backstage/netci-template.yaml as a catalog location.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from netci_gates.client import NetciClient  # noqa: E402
from netci_gates.evidence import (  # noqa: E402
    PROJECT_ROOT,
    EvidenceRecorder,
    GateFailure,
    run_gate,
)

TEMPLATE_FILE = PROJECT_ROOT / "backstage" / "netci-template.yaml"
TEMPLATE_REF = "template:default/netci-application"


class Backstage:
    def __init__(self, base_url: str, *, timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def request(self, method: str, path: str, payload: dict | None = None) -> tuple[int, object]:
        body = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(f"{self.base_url}{path}", data=body, method=method)
        request.add_header("Accept", "application/json")
        if body is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode(errors="replace")
                return response.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode(errors="replace")
            try:
                return exc.code, json.loads(raw)
            except json.JSONDecodeError:
                return exc.code, raw
        except urllib.error.URLError as exc:
            raise GateFailure(f"Backstage is not reachable at {self.base_url}: {exc.reason}") from exc

    def reachable(self) -> bool:
        try:
            status, _ = self.request("GET", "/api/catalog/entities?limit=1")
            return 200 <= status < 300
        except GateFailure:
            return False

    def templates(self) -> list[dict]:
        status, payload = self.request("GET", "/api/catalog/entities?filter=kind=template")
        if status != 200 or not isinstance(payload, list):
            return []
        return payload

    def run_template(self, values: dict) -> str:
        status, payload = self.request(
            "POST", "/api/scaffolder/v2/tasks", {"templateRef": TEMPLATE_REF, "values": values}
        )
        if status not in {200, 201} or not isinstance(payload, dict) or "id" not in payload:
            raise GateFailure(f"scaffolder refused the task: {status} {payload}")
        return str(payload["id"])

    def wait_for_task(self, task_id: str, *, attempts: int = 60, delay: float = 2.0) -> dict:
        for _ in range(attempts):
            status, payload = self.request("GET", f"/api/scaffolder/v2/tasks/{task_id}")
            if status == 200 and isinstance(payload, dict) and payload.get("status") in {
                "completed",
                "failed",
                "cancelled",
            }:
                return payload
            time.sleep(delay)
        raise GateFailure(f"scaffolder task {task_id} did not finish in time")

    def task_events(self, task_id: str) -> list[dict]:
        status, payload = self.request("GET", f"/api/scaffolder/v2/tasks/{task_id}/events")
        if status != 200 or not isinstance(payload, list):
            return []
        return payload


def gate(recorder: EvidenceRecorder) -> None:
    context = recorder.context
    backstage = Backstage(context["backstageUrl"])
    netci = NetciClient(context["apiUrl"], context["pipelineApiKey"])

    recorder.check("Backstage is reachable", backstage.reachable(), detail=backstage.base_url)
    health = netci.health()
    recorder.record("netci-health", health)
    recorder.check_equal("netCI is reachable", health.get("status"), "ok")

    templates = backstage.templates()
    names = [item.get("metadata", {}).get("name") for item in templates]
    recorder.record("catalog-templates", {"names": names})
    recorder.check(
        "the checked-in netCI template is registered in the Backstage catalog",
        "netci-application" in names,
        detail=names,
    )
    template = next(item for item in templates if item["metadata"]["name"] == "netci-application")
    steps = template.get("spec", {}).get("steps", [])
    recorder.check_equal(
        "the template has a single step that calls netCI over HTTP", len(steps), 1
    )
    recorder.check_equal(
        "that step uses the Backstage HTTP action, not a bespoke plugin",
        steps[0].get("action"),
        "http:backstage:request",
    )
    # The action prefixes /api itself, so the template must not repeat it.
    recorder.check(
        "the request goes through the documented proxy endpoint",
        steps[0]["input"]["path"].startswith("/proxy/netci/"),
        detail=steps[0]["input"]["path"],
    )
    recorder.check(
        "the template does not double-prefix /api, which would 404",
        not steps[0]["input"]["path"].startswith("/api/"),
        detail=steps[0]["input"]["path"],
    )

    # ------------------------------------------------------------------- run it
    name = f"bs-{uuid.uuid4().hex[:8]}"
    values = {
        "name": name,
        "repositoryUrl": f"https://github.com/example/{name}",
        "pipelineTemplate": "kubernetes-ci-cd-v1",
        "runtime": "kubernetes",
        "defaultEnvironment": "staging",
    }
    recorder.record("scaffolder-values", values)
    before = {item["name"] for item in netci.expect("GET", "/applications")}

    task_id = backstage.run_template(values)
    recorder.context["taskId"] = task_id
    task = backstage.wait_for_task(task_id)
    events = backstage.task_events(task_id)
    recorder.record(
        "scaffolder-task",
        {
            "id": task_id,
            "status": task.get("status"),
            "log": [
                event.get("body", {}).get("message")
                for event in events
                if isinstance(event, dict) and event.get("type") == "log"
            ][-25:],
        },
    )
    recorder.check_equal("the scaffolder task completed", task.get("status"), "completed")

    # ------------------------------------------------- netCI is the source of truth
    applications = netci.expect("GET", "/applications")
    created = [item for item in applications if item["name"] == name]
    recorder.check(
        "Backstage created the application in netCI, not in a store of its own",
        len(created) == 1,
        detail={"created": name, "newSince": sorted({i["name"] for i in applications} - before)},
    )
    application = created[0]
    recorder.record("netci-application", application)
    recorder.check_equal("the runtime the form asked for was recorded", application["runtime"], "kubernetes")
    recorder.check_equal(
        "the pipeline template the form asked for was recorded",
        application["pipelineTemplate"],
        "kubernetes-ci-cd-v1",
    )
    recorder.check_equal(
        "the default environment the form asked for was recorded",
        application["defaultEnvironment"],
        "staging",
    )
    recorder.check(
        "netCI assigned the identity, so the application is a first-class netCI object",
        bool(application.get("id")) and bool(application.get("createdAt")),
        detail={"id": application.get("id"), "createdAt": application.get("createdAt")},
    )
    recorder.check(
        "the same domain defaults apply as for the Portal: stages come from the template",
        application["stages"]
        == ["checkout", "unit-test", "build", "sbom", "vulnerability-scan", "sign", "publish", "deploy", "health-check"],
        detail=application["stages"],
    )

    # ------------------------------------------------- the domain rules still bind
    invalid = dict(values, name=f"{name}-bad", runtime="docker")
    bad_task = backstage.run_template(invalid)
    bad_result = backstage.wait_for_task(bad_task)
    recorder.record("scaffolder-invalid-task", {"id": bad_task, "status": bad_result.get("status")})
    recorder.check_equal(
        "a runtime that contradicts the template is rejected, the same as from the Portal",
        bad_result.get("status"),
        "failed",
    )
    remaining = [item for item in netci.expect("GET", "/applications") if item["name"] == f"{name}-bad"]
    recorder.check(
        "the rejected request created nothing in netCI",
        not remaining,
        detail=[item["name"] for item in remaining],
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api-url", default=os.getenv("NETCI_API_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--pipeline-api-key", default=os.getenv("NETCI_PIPELINE_API_KEY", "netci-local-pipeline-key"))
    parser.add_argument("--backstage-url", default=os.getenv("NETCI_BACKSTAGE_URL", "http://127.0.0.1:7007"))
    arguments = parser.parse_args()

    if not TEMPLATE_FILE.is_file():
        print(f"missing {TEMPLATE_FILE}", file=sys.stderr)
        return 2

    recorder = EvidenceRecorder(
        "backstage-integration",
        description="A running Backstage instance executes the checked-in Software Template through "
        "the documented proxy; netCI holds the resulting application and still enforces its rules.",
    )
    recorder.context.update(
        {
            "apiUrl": arguments.api_url,
            "pipelineApiKey": arguments.pipeline_api_key,
            "backstageUrl": arguments.backstage_url.rstrip("/"),
        }
    )
    return run_gate(recorder, gate)


if __name__ == "__main__":
    raise SystemExit(main())
