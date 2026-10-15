#!/usr/bin/env python3
"""Active-active, renames and moves (ADR-087): an item renamed or moved on one replica is renamed or
moved on the other, its builds with it, and numbering goes on; a rename while a build runs on the
other replica is refused, with the reason.

    python3 lab/aa/phase9_rename_probe.py

Each client talks to one replica through the VIP, pinned by the balancer's cookie. Steps:
  T1 rename   a job with two builds made on r0, renamed on r1: the old name gone and the new one
              there with both builds on both replicas; the next build is #3
  T2 busy     a job building on r0 (sleep 40), renamed on r1: refused (409, "running on");
              renamed once the build is finished
  T3 move     the renamed job moved into a folder on r1: there on both, builds and all
  T4 folder   the folder renamed on r0, its job inside: there on both under the new name
Evidence: lab/evidence/aa-phase9-rename-<timestamp>.json.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import phase3_probe as p3  # noqa: E402

FOLDER_XML = "<?xml version='1.1' encoding='UTF-8'?><com.cloudbees.hudson.plugins.folder.Folder plugin=\"cloudbees-folder\"/>"
FORM = "application/x-www-form-urlencoded"


def url(name: str) -> str:
    return "".join(f"/job/{urllib.parse.quote(part)}" for part in name.split("/"))


def builds(c: p3.R, name: str) -> dict[int, str]:
    code, body, _ = c.req(url(name) + "/api/json?tree=builds[number,result]")
    if code != 200:
        return {}
    return {b["number"]: b["result"] for b in json.loads(body)["builds"]}


def exists(c: p3.R, name: str) -> bool:
    return c.req(url(name) + "/api/json?tree=name")[0] == 200


def build(c: p3.R, name: str, number: int, timeout: float = 120) -> float | None:
    c.req(url(name) + "/build?delay=0sec", b"", FORM)
    return p3.until(lambda: builds(c, name).get(number) == "SUCCESS", timeout, 0.5)


def everywhere(clients: list[p3.R], gone: str | None, there: str, numbers: list[int], timeout: float = 60) -> float | None:
    """Until every replica has `there` with these builds (all SUCCESS) and no `gone`."""
    def ok() -> bool:
        return all((gone is None or not exists(c, gone)) and all(builds(c, there).get(n) == "SUCCESS" for n in numbers)
                   for c in clients)
    return p3.until(ok, timeout, 0.25)


def main() -> int:
    started = dt.datetime.now(dt.timezone.utc)
    run = started.strftime("%H%M%S")
    r0, r1 = p3.R("r0"), p3.R("r1")
    both = [r0, r1]
    facts: dict = {"started": started.isoformat(), "image": p3.k("-n", p3.NS, "get", "statefulset", "jenkins", "-o",
                                                                   "jsonpath={.spec.template.spec.containers[0].image}")}

    # T1: a job with two builds made on r0, renamed on r1.
    job, renamed = f"rn-{run}", f"rn-{run}-renamed"
    assert r0.create(job) == 200, "create"
    assert p3.until(lambda: exists(r1, job), 30), "the job did not reach r1"
    for n in (1, 2):
        assert build(r0, job, n) is not None, f"build #{n}"
    assert everywhere(both, None, job, [1, 2]) is not None, "the builds did not reach r1"
    at = time.time()
    code, body, served = r1.req(url(job) + "/confirmRename", urllib.parse.urlencode({"newName": renamed}).encode(), FORM)
    took = everywhere(both, job, renamed, [1, 2])
    nxt = build(r0, renamed, 3)
    facts["T1"] = {"code": code, "served": served, "renamedEverywhereAfter": took, "build3": nxt is not None,
                   "builds": {c.replica: builds(c, renamed) for c in both},
                   "oldNameGone": {c.replica: not exists(c, job) for c in both}, "took": round(time.time() - at, 2)}
    print(json.dumps({"T1": facts["T1"]}), flush=True)

    # T2: a rename while the job builds on r0 is refused on r1; done once the build has finished.
    long, long_renamed = f"rn-{run}-long", f"rn-{run}-long-renamed"
    r0.req(f"/createItem?name={long}", p3.JOB_XML.format(d="sleeps").replace("echo 'hi'", "sleep 40").encode(), "application/xml")
    assert p3.until(lambda: exists(r1, long), 30), "the long job did not reach r1"
    r0.req(url(long) + "/build?delay=0sec", b"", FORM)
    assert p3.until(lambda: json.loads(r0.req(url(long) + "/1/api/json?tree=building")[1]).get("building") is True, 60, 0.5), "not building"
    code, body, served = r1.req(url(long) + "/confirmRename", urllib.parse.urlencode({"newName": long_renamed}).encode(), FORM)
    refused = {"code": code, "served": served, "body": body.strip()[:300],
               "stillOldName": {c.replica: exists(c, long) and not exists(c, long_renamed) for c in both}}
    finished = p3.until(lambda: builds(r0, long).get(1) == "SUCCESS", 120, 0.5)
    attempts = []

    def rename_when_finished() -> bool:
        code, body, _ = r1.req(url(long) + "/confirmRename", urllib.parse.urlencode({"newName": long_renamed}).encode(), FORM)
        attempts.append({"code": code, "body": body.strip()[:160] if code >= 400 else ""})
        return code < 400
    done = p3.until(rename_when_finished, 60, 2)
    facts["T2"] = {"whileBuilding": refused, "buildFinished": finished is not None, "renamedAfter": done, "attempts": attempts,
                   "everywhere": everywhere(both, long, long_renamed, [1])}
    print(json.dumps({"T2": facts["T2"]}), flush=True)

    # T3: the renamed job moved into a folder on r1.
    folder = f"rn-{run}-team"
    assert r0.req(f"/createItem?name={folder}", FOLDER_XML.encode(), "application/xml")[0] == 200, "folder"
    assert p3.until(lambda: exists(r1, folder), 30), "the folder did not reach r1"
    code, body, served = r1.req(url(renamed) + "/move/move", urllib.parse.urlencode({"destination": "/" + folder}).encode(), FORM)
    facts["T3"] = {"code": code, "served": served, "everywhere": everywhere(both, renamed, f"{folder}/{renamed}", [1, 2, 3]),
                   "builds": {c.replica: builds(c, f"{folder}/{renamed}") for c in both}}
    print(json.dumps({"T3": facts["T3"]}), flush=True)

    # T4: the folder renamed on r0, its job inside.
    crew = f"rn-{run}-crew"
    code, body, served = r0.req(url(folder) + "/confirmRename", urllib.parse.urlencode({"newName": crew}).encode(), FORM)
    facts["T4"] = {"code": code, "served": served, "everywhere": everywhere(both, folder, f"{crew}/{renamed}", [1, 2, 3]),
                   "next": build(r1, f"{crew}/{renamed}", 4)}
    facts["T4"]["builds"] = {c.replica: builds(c, f"{crew}/{renamed}") for c in both}
    print(json.dumps({"T4": facts["T4"]}), flush=True)

    facts["ok"] = bool(facts["T1"]["renamedEverywhereAfter"] is not None and facts["T1"]["build3"]
                       and refused["code"] == 409 and "running on" in refused["body"] and all(refused["stillOldName"].values())
                       and facts["T2"]["everywhere"] is not None and facts["T3"]["everywhere"] is not None
                       and facts["T4"]["everywhere"] is not None and facts["T4"]["next"] is not None)
    out = p3.p0.ROOT / "lab" / "evidence" / f"aa-phase9-rename-{started.strftime('%Y%m%dT%H%M%SZ')}.json"
    out.write_text(json.dumps(facts, indent=1) + "\n")
    print(f"ok={facts['ok']} evidence {out.relative_to(p3.p0.ROOT)}", flush=True)
    return 0 if facts["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
