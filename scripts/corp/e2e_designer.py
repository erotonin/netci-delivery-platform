#!/usr/bin/env python3
"""Live proof of the pipeline designer (ADR-057) on the corp lab: a proposal opens a real
merge request in the lab GitLab, a reviewer merges it, GitLab's webhook reaches netCI, and the
module's pipeline changes -- with the custom stage's script in the repository, not in netCI.

    set -a; source .netci-gate/real-local.env; set +a
    NETCI_API_URL=http://127.0.0.1:18100 .venv/bin/python scripts/corp/e2e_designer.py propose
    # a person reviews and merges the merge request in GitLab -- the merge is the approval
    NETCI_API_URL=http://127.0.0.1:18100 .venv/bin/python scripts/corp/e2e_designer.py verify

The script never merges: the one who proposes a pipeline change must not be the one who
approves it, and netCI's own GitLab token (api/Developer) cannot merge past approvals either.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(__file__))
from e2e_build import Api, _password_grant, fail, ok, wait_until  # noqa: E402

GITLAB = "http://172.17.0.1:8929/api/v4"
PROJECT = "platform%2Fpayments-api"
STAGE = "lint-dockerfile"
SCRIPT = """#!/usr/bin/env bash
# Added through the netCI pipeline designer (ADR-057), reviewed in a merge request.
set -euo pipefail
test -f Dockerfile
grep -qE '^FROM ' Dockerfile
if grep -qE '^FROM [^ ]+:latest( |$)' Dockerfile; then
  echo 'Dockerfile: base image pinned to :latest' >&2; exit 1
fi
echo "Dockerfile lint passed"
"""


def gitlab(method: str, path: str, body: dict | None = None):
    token = open(".netci-gate/corp/gitlab_admin_token").read().strip()
    request = urllib.request.Request(GITLAB + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"PRIVATE-TOKEN": token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()[:300].decode(errors="replace")


def token_for(var: str) -> str:
    value = os.environ[var]
    user, password = value.split(":", 1) if ":" in value else (value, os.environ["NETCI_LAB_USER_PASSWORD"])
    return _password_grant(user, password)


def main() -> None:
    api = Api(os.environ.get("NETCI_API_URL", "http://127.0.0.1:18100"))
    dev = token_for("NETCI_ACCEPTANCE_USER_DEVELOPER")

    status, pipeline = api.call("GET", "/modules/payments-api/pipeline", token=dev)
    if status != 200:
        fail(f"GET pipeline: {status} {pipeline}")
    if not pipeline["repository"]["supportsProposals"]:
        fail(f"the module's repository does not support proposals: {pipeline['repository']}")
    current = [s["id"] for s in pipeline["stages"]]
    ok(f"current pipeline: {current}")
    if STAGE in current:
        ok(f"{STAGE} is already in the pipeline; nothing to propose")
        return

    stages = []
    for stage in pipeline["stages"]:
        if stage["kind"] == "custom":
            fail("this proof expects a pipeline without custom stages")
        stages.append({"id": stage["id"]})
        if stage["id"] == "unit-test" or (stage["id"] == "checkout" and "unit-test" not in current):
            stages.append({"id": STAGE, "name": "Lint Dockerfile", "after": stage["id"], "code": SCRIPT})
    status, proposal = api.call("POST", "/modules/payments-api/pipeline/proposals", token=dev,
                                body={"title": "add Dockerfile lint", "stages": stages})
    if status != 201:
        fail(f"proposal: {status} {proposal}")
    ok(f"merge request !{proposal['iid']} opened on {proposal['branch']}: {proposal['mergeRequestUrl']}")

    # What the MR changes is what the repository will run: check it before merging.
    # GitLab computes a new merge request's diff asynchronously: an empty list at first.
    def diff_paths():
        st, diffs = gitlab("GET", f"/projects/{PROJECT}/merge_requests/{proposal['iid']}/diffs")
        return sorted(d["new_path"] for d in diffs) if st == 200 and isinstance(diffs, list) and diffs else None
    paths = wait_until(diff_paths, timeout=60, interval=3)
    if paths != [".netci/pipeline.yaml", f".netci/stages/{STAGE}.sh"]:
        fail(f"the merge request changes {paths}")
    ok(f"merge request changes {paths}")

    print(f"next: review and merge {proposal['mergeRequestUrl']} in GitLab, then run: {sys.argv[0]} verify")


def verify() -> None:
    api = Api(os.environ.get("NETCI_API_URL", "http://127.0.0.1:18100"))
    dev = token_for("NETCI_ACCEPTANCE_USER_DEVELOPER")
    admin = token_for("NETCI_ACCEPTANCE_USER_ADMIN")
    status, mrs = gitlab("GET", f"/projects/{PROJECT}/merge_requests?state=merged&order_by=updated_at")
    mr = next((m for m in (mrs if isinstance(mrs, list) else []) if m["source_branch"].startswith("netci/pipeline-")), None)
    if mr is None:
        fail("no merged netci/pipeline-* merge request yet: merge it in GitLab first")
    ok(f"merge request !{mr['iid']} merged by {mr.get('merged_by', {}).get('username')} as {mr.get('merge_commit_sha')}")

    # GitLab's webhook -> netCI: the module's pipeline now carries the stage.
    def applied():
        st, body = api.call("GET", "/modules/payments-api/pipeline", token=dev)
        return body if st == 200 and STAGE in [s["id"] for s in body["stages"]] else None
    after = wait_until(applied, timeout=120, interval=5)
    custom = next(s for s in after["stages"] if s["id"] == STAGE)
    ok(f"pipeline after merge: {[s['id'] for s in after['stages']]}")
    ok(f"custom stage runs {custom['script']} from the repository, after {custom['after']}")

    status, code = api.call("GET", f"/modules/payments-api/pipeline/stages/{STAGE}/code", token=dev)
    if status != 200 or code.get("content") != SCRIPT:
        fail(f"stage code read back from GitLab differs: {status}")
    ok("stage code read back from GitLab matches what was proposed")

    status, audit = api.call("GET", "/audit?limit=50", token=admin)
    events = [e.get("eventType") or e.get("action") for e in (audit.get("items", []) if isinstance(audit, dict) else audit or [])]
    ok(f"audit has proposal_opened={'pipeline.proposal_opened' in events}, "
       f"merge_rejected={'pipeline.merge_rejected' in events}")


if __name__ == "__main__":
    {"propose": main, "verify": verify}.get(sys.argv[1] if len(sys.argv) > 1 else "", lambda: fail("usage: e2e_designer.py propose|verify"))()
