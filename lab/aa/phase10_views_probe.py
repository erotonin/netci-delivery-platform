#!/usr/bin/env python3
"""Active-active, root views (ADR-088): a view made, changed and deleted on one replica is made,
changed and deleted on the other; a job's pages reached through a view are ordered like its own.

    python3 lab/aa/phase10_views_probe.py

Each client talks to one replica through the VIP, pinned by the balancer's cookie. Steps:
  V1 create   a list view made on r0 (from XML, as the API does): on r1 too
  V2 change   a job added to the view on r1, its description set on r1: on r0 too
  V3 through  a job's description set through the view's URL on r0 (/view/<v>/job/<j>/...): on r1
  V4 delete   the view deleted on r0: gone on r1
Evidence: lab/evidence/aa-phase10-views-<timestamp>.json.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import phase3_probe as p3  # noqa: E402

FORM = "application/x-www-form-urlencoded"
VIEW_XML = ("<?xml version='1.1' encoding='UTF-8'?><hudson.model.ListView><name>{n}</name><filterExecutors>false</filterExecutors>"
            "<filterQueue>false</filterQueue><properties class=\"hudson.model.View$PropertyList\"/><jobNames>"
            "<comparator class=\"java.lang.String$CaseInsensitiveComparator\"/></jobNames><jobFilters/><columns/>"
            "<recurse>false</recurse></hudson.model.ListView>")


def view(c: p3.R, name: str) -> dict | None:
    code, body, _ = c.req(f"/view/{urllib.parse.quote(name)}/api/json?tree=name,description,jobs[name]")
    return json.loads(body) if code == 200 else None


def main() -> int:
    started = dt.datetime.now(dt.timezone.utc)
    run = started.strftime("%H%M%S")
    r0, r1 = p3.R("r0"), p3.R("r1")
    v, job = f"vw-{run}", f"vw-{run}-job"
    facts: dict = {"started": started.isoformat()}

    code = r0.req(f"/createView?name={v}", VIEW_XML.format(n=v).encode(), "application/xml")[0]
    facts["V1"] = {"code": code, "onR1After": p3.until(lambda: view(r1, v) is not None, 60, 0.25)}

    assert r0.create(job) == 200 and p3.until(lambda: r1.job(job) is not None, 30), "job"
    added = r1.req(f"/view/{v}/addJobToView", urllib.parse.urlencode({"name": job}).encode(), FORM)[0]
    described = r1.req(f"/view/{v}/submitDescription", urllib.parse.urlencode({"description": "set on r1"}).encode(), FORM)[0]
    facts["V2"] = {"added": added, "described": described, "onR0After": p3.until(
        lambda: (lambda x: x is not None and x.get("description") == "set on r1" and [j["name"] for j in x["jobs"]] == [job])(view(r0, v)),
        60, 0.25)}

    through = r0.req(f"/view/{v}/job/{job}/submitDescription", urllib.parse.urlencode({"description": "through the view"}).encode(), FORM)[0]
    facts["V3"] = {"code": through, "onR1After": p3.until(lambda: (r1.job(job) or {}).get("description") == "through the view", 30, 0.25)}

    deleted = r0.req(f"/view/{v}/doDelete", b"", FORM)[0]
    facts["V4"] = {"code": deleted, "goneOnR1After": p3.until(lambda: view(r1, v) is None, 60, 0.25)}

    facts["ok"] = all(facts[s][k] is not None for s, k in (("V1", "onR1After"), ("V2", "onR0After"), ("V3", "onR1After"),
                                                           ("V4", "goneOnR1After")))
    print(json.dumps(facts, indent=1), flush=True)
    out = p3.p0.ROOT / "lab" / "evidence" / f"aa-phase10-views-{started.strftime('%Y%m%dT%H%M%SZ')}.json"
    out.write_text(json.dumps(facts, indent=1) + "\n")
    print(f"ok={facts['ok']} evidence {out.relative_to(p3.p0.ROOT)}", flush=True)
    return 0 if facts["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
