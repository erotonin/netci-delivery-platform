#!/usr/bin/env python3
"""Active-active, phase 3 (ADR-076): do two replicas with their own homes stay the same, without a
lost update, when both are edited -- and what happens when the store or a replica is gone?

    python3 lab/aa/phase3_probe.py

Needs lab/aa/phase3.yaml applied over phase 2. Each client talks to one replica through the VIP,
pinned by the balancer's cookie. Experiments:
  T1 lag      a job created on r0: how long until r1 has it (10 samples)
  T2 E4       ADR-074 E4 again: a description set on r0, then the job disabled on r1 -- both kept?
  T3 race     40 description edits at once, half on each replica: every edit accepted or refused
              with a reason, none lost silently; both replicas end on the same description
  T4 delete   a job deleted on r1 disappears from r0
  T5 store    netci-homesync scaled to 0: edits refused (503), reads still served; back: edits work
  T6 restart  r1 down while r0 is edited; r1 back: it has every change
  T7 tokens   an API token minted on r0 works on r1 (users are replicated, ADR-077)
  T8 sign-in  a person signed in on r0, r0 killed: is that person still signed in on r1?
Evidence: lab/evidence/aa-phase3-<timestamp>.json.
"""
from __future__ import annotations

import base64
import datetime as dt
import http.cookiejar
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import phase0_probe as p0  # noqa: E402

NS = "aa-p2"
VIP = "http://192.168.122.240"
PASSWORD = (p0.STATE / "cell-admin-password").read_text().strip()
JOB_XML = ("<?xml version='1.1' encoding='UTF-8'?><flow-definition plugin=\"workflow-job\"><description>{d}</description>"
           "<keepDependencies>false</keepDependencies><properties/><definition class=\"org.jenkinsci.plugins.workflow.cps.CpsFlowDefinition\""
           " plugin=\"workflow-cps\"><script>echo 'hi'</script><sandbox>true</sandbox></definition><triggers/>"
           "<disabled>false</disabled></flow-definition>")


class R:
    """A client of one replica, through the VIP, pinned by the balancer's cookie."""

    def __init__(self, replica: str) -> None:
        self.replica = replica
        self.jar = http.cookiejar.CookieJar()
        self.jar.set_cookie(http.cookiejar.Cookie(0, "NETCI_REPLICA", replica, None, False, "192.168.122.240", False, False,
                                                  "/", True, False, None, False, None, None, {}))
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.auth = "Basic " + base64.b64encode(f"admin:{PASSWORD}".encode()).decode()
        self.crumb: tuple[str, str] | None = None

    def req(self, path: str, data: bytes | None = None, ctype: str | None = None, method: str | None = None,
            timeout: float = 15) -> tuple[int, str, str]:
        r = urllib.request.Request(VIP + path, data=data, method=method or ("POST" if data is not None else "GET"))
        r.add_header("Authorization", self.auth)
        if ctype:
            r.add_header("Content-Type", ctype)
        if r.get_method() == "POST":
            if self.crumb is None:
                # A failed fetch is this request's answer, not the writer's end: one thrown here
                # stopped the upgrade probe's r1 writer for the rest of a run (20 writes of 185).
                code, body, served = self.req("/crumbIssuer/api/json")
                if code != 200:
                    return code, body, served
                c = json.loads(body)
                self.crumb = (c["crumbRequestField"], c["crumb"])
            r.add_header(*self.crumb)
        try:
            with self.opener.open(r, timeout=timeout) as resp:
                return resp.status, resp.read().decode(errors="replace"), resp.headers.get("X-Netci-Replica", "")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(errors="replace"), e.headers.get("X-Netci-Replica", "")
        except OSError as e:
            return 0, str(e), ""

    def job(self, name: str) -> dict | None:
        code, body, _ = self.req(f"/job/{urllib.parse.quote(name)}/api/json?tree=description,buildable")
        return json.loads(body) if code == 200 else None

    def create(self, name: str, description: str = "") -> int:
        return self.req(f"/createItem?name={urllib.parse.quote(name)}", JOB_XML.format(d=description).encode(), "application/xml")[0]

    def describe(self, name: str, text: str) -> tuple[int, str]:
        code, body, _ = self.req(f"/job/{urllib.parse.quote(name)}/submitDescription",
                                 urllib.parse.urlencode({"description": text}).encode(), "application/x-www-form-urlencoded")
        return code, body.strip()[:200]


def until(cond, timeout: float = 60, every: float = 0.05) -> float | None:
    start = time.time()
    while time.time() - start < timeout:
        try:
            if cond():
                return time.time() - start
        except Exception:  # noqa: BLE001 - keep polling
            pass
        time.sleep(every)
    return None


def k(*args: str, check: bool = True) -> str:
    return p0.k(*args, check=check)


def main() -> int:
    started = dt.datetime.now(dt.timezone.utc)
    tag = started.strftime("%H%M%S")
    facts: dict = {"scenario": "aa-phase3", "started": started.isoformat(timespec="seconds")}
    r0, r1 = R("r0"), R("r1")
    assert r0.req("/api/json")[2] == "r0" and r1.req("/api/json")[2] == "r1", "the cookie did not pin the replicas"

    # T1: lag of a create, r0 -> r1.
    lags = []
    for i in range(10):
        name = f"lag-{tag}-{i}"
        t = time.time()
        assert r0.create(name) == 200, name
        seen = until(lambda: r1.job(name) is not None, 30)
        lags.append(round((time.time() - t) * 1000) if seen is not None else None)
    ok = sorted(x for x in lags if x is not None)
    facts["T1"] = {"samplesMs": lags, "medianMs": ok[len(ok) // 2] if ok else None, "maxMs": max(ok) if ok else None,
                   "missing": lags.count(None)}
    print("T1", facts["T1"], flush=True)

    # T2: ADR-074 E4 again.
    name = f"e4-{tag}"
    r0.create(name)
    until(lambda: r1.job(name) is not None, 30)
    d = r0.describe(name, "set on r0")
    disabled = r1.req(f"/job/{name}/disable", b"", "application/x-www-form-urlencoded")[0]
    until(lambda: r0.job(name) and r0.job(name)["buildable"] is False, 30)
    facts["T2"] = {"describeOnR0": d[0], "disableOnR1": disabled, "r0": r0.job(name), "r1": r1.job(name),
                   "noLostUpdate": all(j and j["description"] == "set on r0" and j["buildable"] is False for j in (r0.job(name), r1.job(name)))}
    print("T2", facts["T2"], flush=True)

    # T3: a race of edits on both replicas.
    name = f"race-{tag}"
    r0.create(name)
    until(lambda: r1.job(name) is not None, 30)
    clients = [R("r0") for _ in range(4)] + [R("r1") for _ in range(4)]

    def edit(i: int) -> tuple[int, int, str]:
        c = clients[i % len(clients)]
        code, body = c.describe(name, f"edit {i} via {c.replica}")
        return i, code, body

    with ThreadPoolExecutor(8) as ex:
        results = list(ex.map(edit, range(40)))
    time.sleep(3)
    j0, j1 = r0.job(name), r1.job(name)
    accepted = [i for i, code, _ in results if code == 200]
    refused = [(i, code, body) for i, code, body in results if code != 200]
    facts["T3"] = {"edits": 40, "accepted": len(accepted), "refused": len(refused),
                   "refusalReasons": sorted({b.split(";")[0][:90] for _, _, b in refused}),
                   "refusalCodes": sorted({c for _, c, _ in refused}),
                   "r0Description": j0 and j0["description"], "r1Description": j1 and j1["description"],
                   "converged": bool(j0 and j1 and j0["description"] == j1["description"]),
                   "finalIsAnAcceptedEdit": bool(j0 and any(j0["description"] == f"edit {i} via {clients[i % len(clients)].replica}" for i in accepted))}
    print("T3", facts["T3"], flush=True)

    # T4: delete propagates.
    name = f"del-{tag}"
    r0.create(name)
    until(lambda: r1.job(name) is not None, 30)
    code = r1.req(f"/job/{name}/doDelete", b"", "application/x-www-form-urlencoded")[0]
    gone = until(lambda: r0.job(name) is None, 30)
    facts["T4"] = {"deleteOnR1": code, "goneFromR0AfterSeconds": round(gone, 2) if gone is not None else None}
    print("T4", facts["T4"], flush=True)

    # T5: the store gone.
    name = f"store-{tag}"
    r0.create(name)
    until(lambda: r1.job(name) is not None, 30)
    k("-n", NS, "scale", "deployment/homesync", "--replicas=0")
    until(lambda: k("-n", NS, "get", "pods", "-l", "app=homesync", "-o", "name", check=False) == "", 60, 1)
    edit_down = r0.describe(name, "while the store is down")
    read_down = (r0.req(f"/job/{name}/api/json")[0], r1.req("/api/json")[0])
    k("-n", NS, "scale", "deployment/homesync", "--replicas=2")
    k("-n", NS, "rollout", "status", "deployment/homesync", "--timeout=120s")
    back = until(lambda: r0.describe(name, "after the store came back")[0] == 200, 60, 1)
    synced = until(lambda: r1.job(name)["description"] == "after the store came back", 30)
    facts["T5"] = {"editWhileDown": edit_down, "readsWhileDown": read_down,
                   "editAcceptedAfterSeconds": round(back, 1) if back is not None else None,
                   "r1HasItAfterSeconds": round(synced, 2) if synced is not None else None,
                   "r0DescriptionAfterOutage": r0.job(name)["description"]}
    print("T5", facts["T5"], flush=True)

    # T6: a replica down while the other is edited.
    k("-n", NS, "scale", "statefulset/jenkins", "--replicas=1")  # jenkins-1 (r1) goes
    until(lambda: k("-n", NS, "get", "pod", "jenkins-1", "-o", "name", check=False) == "", 120, 1)
    made = []
    for i in range(3):
        n = f"while-down-{tag}-{i}"
        made.append((n, r0.create(n, f"made while r1 was down {i}")))
    edited = r0.describe(f"e4-{tag}", "edited while r1 was down")
    k("-n", NS, "scale", "statefulset/jenkins", "--replicas=2")
    back = until(lambda: R("r1").req("/api/json")[2] == "r1" and R("r1").req("/api/json")[0] == 200, 600, 2)
    r1 = R("r1")
    has = {n: (r1.job(n) or {}).get("description") for n, _ in made}
    facts["T6"] = {"createdOnR0": made, "editOnR0": edited[0], "r1BackAfterSeconds": round(back, 1) if back is not None else None,
                   "r1Has": has, "r1E4Description": (r1.job(f"e4-{tag}") or {}).get("description"),
                   "complete": all(v == f"made while r1 was down {i}" for i, v in enumerate(has.values()))
                   and (r1.job(f"e4-{tag}") or {}).get("description") == "edited while r1 was down"}
    print("T6", facts["T6"], flush=True)

    # T7: an API token minted on r0, used on r1. Kept in memory only, revoked at the end.
    code, body, _ = r0.req("/user/admin/descriptorByName/jenkins.security.ApiTokenProperty/generateNewToken",
                           urllib.parse.urlencode({"newTokenName": f"probe-{tag}"}).encode(), "application/x-www-form-urlencoded")
    data = json.loads(body).get("data", {}) if code == 200 else {}
    token, uuid = data.get("tokenValue"), data.get("tokenUuid")
    t7: dict = {"minted": code}
    if token:
        by_token = "Basic " + base64.b64encode(f"admin:{token}".encode()).decode()

        def with_token(replica: str) -> int:
            c = R(replica)
            c.auth = by_token
            return c.req("/api/json?tree=mode")[0]

        t7["acceptedOnR1AfterSeconds"] = until(lambda: with_token("r1") == 200, 30, 0.2)
        t7["onR0"], t7["onR1"] = with_token("r0"), with_token("r1")
        r0.req("/user/admin/descriptorByName/jenkins.security.ApiTokenProperty/revoke",
               urllib.parse.urlencode({"tokenUuid": uuid}).encode(), "application/x-www-form-urlencoded")
        t7["refusedOnR1AfterRevokeSeconds"] = until(lambda: with_token("r1") == 401, 30, 0.2)
        del token, by_token
    facts["T7"] = t7
    print("T7", t7, flush=True)

    # T8: a sign-in through the form on r0, then r0 killed (no graceful stop).
    sys.path.insert(0, str(Path(__file__).parent))
    import phase2_probe as p2  # the form sign-in with its crumb

    person = p2.Session()
    person.jar.set_cookie(http.cookiejar.Cookie(0, "NETCI_REPLICA", "r0", None, False, "192.168.122.240", False, False,
                                                "/", True, False, None, False, None, None, {}))
    pinned = person.sign_in()
    shared = any(c.name == "NETCI_SESSION" for c in person.jar)
    if not shared:  # issued on the first request after the sign-in
        person.who()
        shared = any(c.name == "NETCI_SESSION" for c in person.jar)
    k("-n", NS, "delete", "pod", "jenkins-0", "--grace-period=0", "--force", check=False)
    seen = []
    t0 = time.time()
    while time.time() - t0 < 30:
        code, rep, user, _ = person.who()
        seen.append({"t": round(time.time() - t0, 1), "code": code, "replica": rep, "user": user})
        time.sleep(0.5)
    on_r1 = [s for s in seen if s["replica"] == "r1" and s["code"] == 200]
    facts["T8"] = {"pinnedTo": pinned, "sharedCookieIssued": shared, "polls": len(seen),
                   "answeredByR1": len(on_r1), "signedInOnR1": bool(on_r1) and all(s["user"] == "admin" for s in on_r1),
                   "usersSeenOnR1": sorted({s["user"] for s in on_r1}), "failures": sum(1 for s in seen if s["code"] != 200)}
    print("T8", facts["T8"], flush=True)
    k("-n", NS, "rollout", "status", "statefulset/jenkins", "--timeout=600s", check=False)

    out = p0.EVIDENCE / f"aa-phase3-{started.strftime('%Y%m%dT%H%M%SZ')}.json"
    out.write_text(json.dumps(facts, indent=2) + "\n")
    print("evidence:", out.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
