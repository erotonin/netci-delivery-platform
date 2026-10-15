#!/usr/bin/env python3
"""What a real GitLab does to the IDP on the HA lab: merge requests, forks, superseded builds, paths.

    set -a; source .netci-gate/aa/idp.env; set +a
    python3 lab/aa/idp_scm_probe.py mr        a merge request from a branch of platform/payments-api
    python3 lab/aa/idp_scm_probe.py fork      one from a fork: ignored by default, verify-only when allowed
    python3 lab/aa/idp_scm_probe.py cancel    a new commit to a merge request while its build runs
    python3 lab/aa/idp_scm_probe.py paths     a push to main that changes only documentation

Everything goes through GitLab's API -- branches, commits, forks and merge requests made as GitLab
users, the project's webhook signed with the module's secret -- and is read back from the IDP's API,
Jenkins and Harbor. Nothing calls the IDP's webhook route directly. The outsider who forks is a GitLab
user made here, acting through an impersonation token the admin makes (no password is ever set or stored). Evidence:
lab/evidence/aa-idp-scm-<scenario>-<timestamp>.json. Nothing secret is printed.
"""
from __future__ import annotations

import base64
import json
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import idp_e2e as e  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
GITLAB = "http://172.17.0.1:8929/api/v4"
PROJECT = "platform%2Fpayments-api"
OUTSIDER = "outsider"
FACTS: dict = {"steps": []}


def user_token(username: str) -> str:
    """An impersonation token of a GitLab user, made once by the admin and kept in .netci-gate (600):
    the admin token has no sudo scope, and the user's password is never set."""
    path = ROOT / f".netci-gate/aa/gitlab-{username}-token"
    if not path.exists():
        code, users = gl("GET", f"/users?username={username}")
        code, made = gl("POST", f"/users/{users[0]['id']}/impersonation_tokens",
                        {"name": "netci-scm-probe", "scopes": ["api"],
                         "expires_at": time.strftime("%Y-%m-%d", time.gmtime(time.time() + 30 * 86400))})
        if code != 201:
            raise SystemExit(f"impersonation token for {username} refused: {code}")
        import os
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(fd, made["token"].encode())
        os.close(fd)
    return path.read_text().strip()


def gl(method: str, path: str, body=None, sudo: str | None = None):
    token = user_token(sudo) if sudo else (ROOT / ".netci-gate/corp/gitlab_admin_token").read_text().strip()
    headers = {"PRIVATE-TOKEN": token, "Content-Type": "application/json"}
    req = urllib.request.Request(GITLAB + path, method=method, headers=headers,
                                 data=None if body is None else json.dumps(body).encode())
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as x:
        return x.code, x.read().decode(errors="replace")[:300]


def note(what: str, ok: bool, **facts) -> None:
    FACTS["steps"].append({"step": what, "ok": ok, **facts})
    print(f"[{'ok' if ok else 'FAIL'}] {what}: {json.dumps(facts)[:300]}", flush=True)


def commit(project: str, branch: str, message: str, path: str, content: str, *, start: str | None = None, sudo=None) -> str:
    code, existing = gl("GET", f"/projects/{project}/repository/files/{urllib.parse.quote(path, safe='')}?ref={start or branch}", sudo=sudo)
    action = "update" if code == 200 else "create"
    body = {"branch": branch, "commit_message": message, "actions": [{"action": action, "file_path": path, "content": content}]}
    if start:
        body["start_branch"] = start
    code, c = gl("POST", f"/projects/{project}/repository/commits", body, sudo=sudo)
    if code != 201:
        raise SystemExit(f"commit to {project}:{branch} refused: {code} {c}")
    return c["id"]


def runs_for(token: str, sha: str) -> list[dict]:
    code, body = e.call("GET", "/modules/payments-api/pipeline-runs", token)
    return [r for r in (body.get("items") or []) if r["commitSha"] == sha] if code == 200 else []


def wait_runs(token: str, sha: str, seconds: int = 60, at_least: int = 1) -> list[dict]:
    end = time.time() + seconds
    while time.time() < end:
        found = runs_for(token, sha)
        if len(found) >= at_least:
            return found
        time.sleep(2)
    return runs_for(token, sha)


def wait_terminal(token: str, run_id: str, seconds: int = 900) -> dict:
    end = time.time() + seconds
    while time.time() < end:
        code, run = e.call("GET", f"/pipeline-runs/{run_id}", token)
        if code == 200 and run["status"] in ("succeeded", "failed", "cancelled", "superseded"):
            return run
        time.sleep(3)
    return e.call("GET", f"/pipeline-runs/{run_id}", token)[1]


def stage(token: str, run_id: str, stage_id: str) -> str | None:
    code, body = e.call("GET", f"/pipeline-runs/{run_id}/stages", token)
    return next((s["status"] for s in body.get("items", []) if s["stageId"] == stage_id), None) if code == 200 else None


def statuses(sha: str) -> list[dict]:
    code, body = gl("GET", f"/projects/{PROJECT}/repository/commits/{sha}/statuses")
    return [{"name": s["name"], "status": s["status"], "target": s.get("target_url")} for s in body] if code == 200 else []


def harbor_has_tag(tag: str) -> bool:
    name = (ROOT / ".netci-gate/corp/harbor_robot_name").read_text().strip()
    secret = (ROOT / ".netci-gate/corp/harbor_robot_secret").read_text().strip()
    ctx = ssl.create_default_context(cafile=str(ROOT / ".netci-gate/corp/pki/ca.crt"))
    req = urllib.request.Request(f"https://172.17.0.1:8930/api/v2.0/projects/apps/repositories/payments-api/artifacts/{tag}/tags",
                                 headers={"Authorization": "Basic " + base64.b64encode(f"{name}:{secret}".encode()).decode()})
    try:
        with urllib.request.urlopen(req, timeout=20, context=ctx) as r:
            return r.status == 200 and any(t.get("name") == tag for t in json.loads(r.read()))
    except urllib.error.HTTPError as x:
        if x.code == 404:
            return False
        raise


def summary(run: dict) -> dict:
    t = run.get("trigger") or {}
    return {"run": run["id"][:8], "status": run["status"], "event": t.get("event"), "fromFork": t.get("fromFork"),
            "pullRequest": t.get("pullRequest"), "reason": t.get("reason"), "deployAfterBuild": run.get("deployAfterBuild"),
            "publishArtifact": run.get("publishArtifact"), "digest": (run.get("artifactDigest") or "")[:19] or None,
            "supersededBy": run.get("supersededBy")}


def set_rules(admin: str, delivery: dict) -> None:
    code, revs = e.call("GET", "/modules/payments-api/config-revisions", admin)
    items = revs.get("items", revs) if isinstance(revs, dict) else revs
    active = next(r for r in items if r.get("status") == "active")
    code, module = e.call("GET", "/modules/payments-api", admin)
    pipeline = dict(active["pipelineConfig"])
    pipeline["delivery"] = delivery
    code, body = e.call("POST", "/modules/payments-api/config-revisions", admin,
                        {"pipelineConfig": pipeline, "deploymentConfig": active["deploymentConfig"],
                         "changeSummary": "delivery rules for the SCM probe", "expectedVersion": module["configVersion"]})
    note("delivery rules set through a config revision", code == 201, code=code, status=body.get("status") if isinstance(body, dict) else body)
    if code != 201:
        raise SystemExit(1)


DEFAULT_TRIGGERS = [{"on": "push", "branches": ["main"], "deployTo": "dev"}, {"on": "push", "branches": ["**"]},
                    {"on": "pull_request", "branches": ["**"]}, {"on": "tag", "tags": ["v*"], "registerVersion": True}]


def merge_request(source_project: str, branch: str, title: str, sudo=None) -> dict:
    body = {"source_branch": branch, "target_branch": "main", "title": title}
    if source_project != PROJECT:
        code, target = gl("GET", f"/projects/{PROJECT}")
        body["target_project_id"] = target["id"]
    code, mr = gl("POST", f"/projects/{source_project}/merge_requests", body, sudo=sudo)
    if code != 201:
        raise SystemExit(f"merge request refused: {code} {mr}")
    return mr


def scenario_mr(dev: str) -> None:
    branch = f"feature/mr-{int(time.time())}"
    sha = commit(PROJECT, branch, "feat: say which branch answered", "app.py", _app_with(f"# built from {branch}\n"), start="main")
    pushed = wait_runs(dev, sha, 60)
    note("the branch push made a build-only run", len(pushed) == 1 and pushed[0]["deployAfterBuild"] is False,
         runs=[summary(r) for r in pushed])
    mr = merge_request(PROJECT, branch, "Say which branch answered")
    time.sleep(20)  # the merge request's webhook has long arrived by now
    both = runs_for(dev, sha)
    code, logs = e.call("GET", f"/pipeline-runs/{pushed[0]['id']}/logs", dev) if pushed else (0, {})
    lines = [str(x.get("message", x) if isinstance(x, dict) else x) for x in (logs.get("items") or logs.get("lines") or [])] if code == 200 else []
    note("the merge request joined the push's build: one commit, one build (ADR-095)",
         len(both) == 1 and any("joined this build" in l for l in lines), mr=mr["iid"], runs=[summary(r) for r in both],
         joined=[l for l in lines if "joined this build" in l])
    for r in both:
        done = wait_terminal(dev, r["id"])
        note(f"run {r['id'][:8]} ended", done["status"] == "succeeded", **summary(done))
    note("no run of this commit deployed anywhere", all(r.get("deployAfterBuild") is False for r in runs_for(dev, sha)))
    # Statuses go out through the outbox: the final one follows the run by a few seconds.
    end = time.time() + 60
    while time.time() < end and not any(s["status"] in ("success", "failed") for s in statuses(sha)):
        time.sleep(3)
    st = statuses(sha)
    note("the commit's status on GitLab", any(s["status"] in ("success", "failed") for s in st), statuses=st)
    FACTS.update({"mr": mr["iid"], "branch": branch, "commit": sha})


_APP = None


def _app_with(extra: str) -> str:
    global _APP
    if _APP is None:
        req = urllib.request.Request(f"{GITLAB}/projects/{PROJECT}/repository/files/app.py/raw?ref=main",
                                     headers={"PRIVATE-TOKEN": (ROOT / ".netci-gate/corp/gitlab_admin_token").read_text().strip()})
        _APP = urllib.request.urlopen(req, timeout=20).read().decode()
    return _APP + extra


def scenario_fork(dev: str, admin: str) -> None:
    code, users = gl("GET", f"/users?username={OUTSIDER}")
    if not users:
        code, user = gl("POST", "/users", {"username": OUTSIDER, "name": "An outside contributor", "email": "outsider@example.invalid",
                                           "force_random_password": True, "skip_confirmation": True})
        note("GitLab user outsider made (random password, never stored)", code == 201, code=code)
    code, forks = gl("GET", f"/projects/{OUTSIDER}%2Fpayments-api")
    if code != 200:
        code, fork = gl("POST", f"/projects/{PROJECT}/fork", {"namespace_path": OUTSIDER}, sudo=OUTSIDER)
        note("outsider forked platform/payments-api", code in (201, 409), code=code)
        time.sleep(8)
    fork_project = f"{OUTSIDER}%2Fpayments-api"
    set_rules(admin, {"triggers": DEFAULT_TRIGGERS, "forkPullRequests": "ignore"})
    branch = f"fix/outside-{int(time.time())}"
    sha = commit(fork_project, branch, "fix: an outside change", "app.py", _app_with("# an outside change\n"), start="main", sudo=OUTSIDER)
    mr = merge_request(fork_project, branch, "An outside change", sudo=OUTSIDER)
    ignored = wait_runs(dev, sha, 45)
    note("forkPullRequests=ignore: the fork's merge request built nothing", ignored == [], mr=mr["iid"], runs=[summary(r) for r in ignored])

    set_rules(admin, {"triggers": DEFAULT_TRIGGERS, "forkPullRequests": "verify"})
    sha2 = commit(fork_project, branch, "fix: the outside change, again", "app.py", _app_with("# an outside change, again\n"), sudo=OUTSIDER)
    verify = wait_runs(dev, sha2, 60)
    note("forkPullRequests=verify: one run, verify only", len(verify) == 1 and verify[0].get("publishArtifact") is False
         and verify[0].get("deployAfterBuild") is False, runs=[summary(r) for r in verify])
    if verify:
        done = wait_terminal(dev, verify[0]["id"])
        note("the fork's verify run ended", done["status"] == "succeeded", **summary(done),
             sign=stage(dev, done["id"], "sign"), publish=stage(dev, done["id"], "publish"))
        note("no registry tag for the fork's commit", not harbor_has_tag(sha2), tag=sha2[:12])
    set_rules(admin, {"triggers": DEFAULT_TRIGGERS, "forkPullRequests": "ignore"})
    FACTS.update({"mr": mr["iid"], "commits": [sha, sha2]})


def scenario_cancel(dev: str) -> None:
    branch = f"feature/supersede-{int(time.time())}"
    sha1 = commit(PROJECT, branch, "feat: first try", "app.py", _app_with("# first try\n"), start="main")
    mr = merge_request(PROJECT, branch, "Supersede a running build")
    first = wait_runs(dev, sha1, 60)
    if not first:
        raise SystemExit("no run for the first commit")
    rid = first[0]["id"]
    end = time.time() + 300
    while time.time() < end and stage(dev, rid, "build") != "running":
        time.sleep(1)
    note("the first commit's build is running", stage(dev, rid, "build") == "running", run=rid[:8])
    sha2 = commit(PROJECT, branch, "feat: second try", "app.py", _app_with("# second try\n"))
    second = wait_runs(dev, sha2, 60)
    time.sleep(10)
    note("the new commit built once", len(runs_for(dev, sha2)) == 1, runs=[summary(r) for r in runs_for(dev, sha2)])
    old = wait_terminal(dev, rid, 300)
    note("the running build was superseded by the new commit", old["status"] in ("cancelled", "superseded"), **summary(old))
    if second:
        new = wait_terminal(dev, second[0]["id"])
        note("the new commit's build ended", new["status"] == "succeeded", **summary(new))
    page = old.get("consoleUrl")
    if page:
        import phase3_probe as p3
        path = "/" + page.split("://", 1)[1].split("/", 1)[1]
        best = {}
        for r in ("r0", "r1"):
            code, body, served = p3.R(r).req(path + "api/json?tree=result,building")
            if code == 200:
                best = json.loads(body)
        note("Jenkins stopped the old build", best.get("result") in ("ABORTED", "NOT_BUILT") and not best.get("building"), jenkins=best)
    FACTS.update({"mr": mr["iid"], "commits": [sha1, sha2]})


def scenario_paths(dev: str, admin: str) -> None:
    # Both push rules: main's deploy and every branch's build. With only main's, a docs-only push
    # to main fell through to the build-only rule and built anyway.
    docs = ["**/*.md", "docs/**"]
    triggers = [dict(DEFAULT_TRIGGERS[0], pathsIgnore=docs), dict(DEFAULT_TRIGGERS[1], pathsIgnore=docs)] + DEFAULT_TRIGGERS[2:]
    set_rules(admin, {"triggers": triggers, "forkPullRequests": "ignore"})
    docs_commit = commit(PROJECT, "main", "docs: a note for readers", "README.md",
                  urllib.request.urlopen(urllib.request.Request(f"{GITLAB}/projects/{PROJECT}/repository/files/README.md/raw?ref=main",
                                         headers={"PRIVATE-TOKEN": (ROOT / ".netci-gate/corp/gitlab_admin_token").read_text().strip()}),
                                         timeout=20).read().decode() + "\nA note for readers.\n")
    found = wait_runs(dev, docs_commit, 45)
    note("a docs-only push to main built nothing", found == [], runs=[summary(r) for r in found])
    code = commit(PROJECT, "main", "feat: answer with the version", "app.py", _app_with("# a code change\n"))
    found = wait_runs(dev, code, 45)
    note("a code push to main deploys to dev", any(r.get("deployAfterBuild") for r in found), runs=[summary(r) for r in found])
    for r in found:
        done = wait_terminal(dev, r["id"])
        note(f"run {r['id'][:8]} ended", done["status"] == "succeeded", **summary(done))
    FACTS.update({"commits": [docs_commit, code], "rules": triggers})


def main() -> int:
    which = sys.argv[1] if len(sys.argv) > 1 else ""
    dev = e.token("NETCI_ACCEPTANCE_USER_DEVELOPER")
    admin = e.token("NETCI_ACCEPTANCE_USER_ADMIN")
    if which == "mr":
        scenario_mr(dev)
    elif which == "fork":
        scenario_fork(dev, admin)
    elif which == "cancel":
        scenario_cancel(dev)
    elif which == "paths":
        scenario_paths(dev, admin)
    else:
        print(__doc__)
        return 2
    out = ROOT / f"lab/evidence/aa-idp-scm-{which}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json"
    out.write_text(json.dumps(FACTS, indent=2) + "\n")
    print(f"evidence {out.relative_to(ROOT)}")
    return 0 if all(s["ok"] for s in FACTS["steps"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
