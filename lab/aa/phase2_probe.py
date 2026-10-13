#!/usr/bin/env python3
"""Active-active, phase 2 (ADR-076): do the UI and the API keep answering through the balancer
when a machine loses power?

    python3 lab/aa/phase2_probe.py [--observe 150]

Needs lab/aa/phase2.yaml applied (namespace aa-p2: two replicas on local-path, an HAProxy per machine, the
VIP 192.168.122.240 held by kube-vip). Three probes run every 0.5 s through the VIP, each request
with a 2 s timeout:
  login    GET /login, no session -- the page a person lands on;
  api      GET /api/json with basic auth, a new connection each time -- what scripts and netCI do;
  session  a person signed in through the form, cookies kept -- does the sign-in survive?
Scenarios, each followed by the machine powered back on and the stack whole again:
  replica  the machine of the replica the session is pinned to, which does not hold the VIP;
  vip      the machine holding the VIP, with the session on a replica elsewhere;
  both     the VIP's machine when it also runs the session's replica (skipped, saying so, if not).
Evidence: lab/evidence/aa-phase2-<timestamp>.json.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import http.cookiejar
import json
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import phase0_probe as p0  # noqa: E402  (kubectl wrapper, paths)

NS = "aa-p2"
VIP = "http://192.168.122.240"
VIRSH = ["virsh", "-c", "qemu:///system"]
PASSWORD = (p0.STATE / "cell-admin-password").read_text().strip()
AUTH = "Basic " + base64.b64encode(f"admin:{PASSWORD}".encode()).decode()


def k(*args: str, check: bool = True) -> str:
    return p0.k(*args, check=check)


def request(opener, path: str, headers: dict | None = None, data: bytes | None = None) -> tuple[int, str, float]:
    """(status, replica that answered, seconds). Status 0: no answer within 2 s."""
    req = urllib.request.Request(VIP + path, data=data, headers=headers or {})
    start = time.time()
    try:
        with opener.open(req, timeout=2) as resp:
            resp.read()
            return resp.status, resp.headers.get("X-Netci-Replica", ""), time.time() - start
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("X-Netci-Replica", ""), time.time() - start
    except Exception:  # noqa: BLE001 - timeout, refused, reset: all "did not answer"
        return 0, "", time.time() - start


class Session:
    """A person: signs in through the form; the balancer's cookie pins them to one replica."""

    def __init__(self) -> None:
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def sign_in(self) -> str:
        # The form carries a crumb bound to the session the login page opened (and so to the
        # replica the balancer pinned it to): without it Jenkins answers /loginError.
        with self.opener.open(VIP + "/login", timeout=5) as resp:
            page = resp.read().decode()
        match = re.search(r'name="Jenkins-Crumb" type="hidden" value="([^"]+)"', page)
        fields = {"j_username": "admin", "j_password": PASSWORD, "from": "/"}
        if match:
            fields["Jenkins-Crumb"] = match.group(1)
        form = urllib.parse.urlencode(fields).encode()
        request(self.opener, "/j_spring_security_check", {"Content-Type": "application/x-www-form-urlencoded"}, form)
        return self.who()[1]

    def who(self) -> tuple[int, str, str, float]:
        req = urllib.request.Request(VIP + "/whoAmI/api/json")
        start = time.time()
        try:
            with self.opener.open(req, timeout=2) as resp:
                body = json.loads(resp.read())
                name = body.get("name") if body.get("authenticated") and body.get("name") != "anonymous" else "anonymous"
                return resp.status, resp.headers.get("X-Netci-Replica", ""), name, time.time() - start
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("X-Netci-Replica", ""), "", time.time() - start
        except Exception:  # noqa: BLE001
            return 0, "", "", time.time() - start


def summarise(samples: list[dict], t_loss: float) -> dict:
    ok = [s for s in samples if s["code"] == 200]
    failures = [s for s in samples if s["code"] != 200]
    longest, start = 0.0, None
    for s in samples:
        if s["code"] != 200:
            start = start if start is not None else s["t"]
            longest = max(longest, s["t"] + s["secs"] - start)
        else:
            start = None
    lat = sorted(s["secs"] for s in ok)
    return {"polls": len(samples), "failures": len(failures), "longestOutageSeconds": round(longest, 2),
            "firstFailureAfterLossSeconds": round(failures[0]["t"] - t_loss, 2) if failures else None,
            "p50ms": round(lat[len(lat) // 2] * 1000, 1) if lat else None,
            "p99ms": round(lat[max(0, int(len(lat) * 0.99) - 1)] * 1000, 1) if lat else None,
            "answeredBy": {r: sum(1 for s in ok if s["replica"] == r) for r in sorted({s["replica"] for s in ok})}}


def where() -> dict:
    pods = json.loads(k("-n", NS, "get", "pods", "-o", "json"))["items"]
    roles: dict[str, list[str]] = {}
    for pod in pods:
        roles.setdefault(pod["spec"].get("nodeName", "?"), []).append(pod["metadata"]["name"])
    holder = k("-n", NS, "get", "lease", "kubevip-ui", "-o", "jsonpath={.spec.holderIdentity}", check=False)
    return {"pods": roles, "vipHolder": holder}


def replica_node(replica: str) -> str:
    return k("-n", NS, "get", "pod", f"jenkins-{replica[1:]}", "-o", "jsonpath={.spec.nodeName}")


def whole_again(timeout: float = 900) -> float:
    """Every node Ready, every pod ready, both replicas answering through the VIP."""
    start = time.time()
    while time.time() - start < timeout:
        nodes = k("get", "nodes", "-o", "jsonpath={range .items[*]}{.status.conditions[?(@.type=='Ready')].status}{' '}{end}", check=False)
        ready = k("-n", NS, "get", "pods", "-o", "jsonpath={range .items[*]}{.status.containerStatuses[0].ready}{' '}{end}", check=False)
        seen = {request(urllib.request.build_opener(), "/login")[1] for _ in range(6)}
        vip = k("-n", "kube-system", "get", "ds", "kube-vip", "-o", "jsonpath={.status.numberReady}", check=False)
        if nodes.split() == ["True"] * 3 and ready.split() and set(ready.split()) == {"true"} and seen >= {"r0", "r1"} and vip == "3":
            # Settle: a pod is ready before kube-vip has seen its endpoint; the next loss must not
            # land in that gap (it did once, and was measured as a slow failover).
            time.sleep(45)
            return time.time() - start
        time.sleep(5)
    raise TimeoutError("the stack did not come back whole")


def scenario(name: str, victim: str, session: Session, pinned: str, observe: float) -> dict:
    before = where()
    samples: dict[str, list[dict]] = {"login": [], "api": [], "session": []}
    stop = threading.Event()
    plain = urllib.request.build_opener()

    def probe(kind: str) -> None:
        while not stop.is_set():
            t = time.time()
            if kind == "login":
                code, rep, secs = request(plain, "/login")
                samples[kind].append({"t": t, "code": code, "replica": rep, "secs": secs})
            elif kind == "api":
                code, rep, secs = request(plain, "/api/json?tree=jobs[name]", {"Authorization": AUTH})
                samples[kind].append({"t": t, "code": code, "replica": rep, "secs": secs})
            else:
                code, rep, user, secs = session.who()
                samples[kind].append({"t": t, "code": code, "replica": rep, "user": user, "secs": secs})
            time.sleep(max(0.0, 0.5 - (time.time() - t)))

    threads = [threading.Thread(target=probe, args=(kind,), daemon=True) for kind in samples]
    for th in threads:
        th.start()
    time.sleep(10)
    t_loss = time.time()
    subprocess.run([*VIRSH, "destroy", victim], check=True, capture_output=True)
    print(f"[{name}] power lost: {victim}", flush=True)
    time.sleep(observe)
    stop.set()
    for th in threads:
        th.join(5)
    after = where()
    sess = samples["session"]
    signed_in_after = [s for s in sess if s["t"] > t_loss + 15 and s["code"] == 200]
    facts = {
        "victim": victim, "before": before, "afterObserving": after, "sessionPinnedTo": pinned,
        **{kind: summarise(rows, t_loss) for kind, rows in samples.items()},
        "sessionStillSignedIn": bool(signed_in_after) and all(s["user"] == "admin" for s in signed_in_after),
        "sessionUsersAfter": sorted({s["user"] for s in signed_in_after}),
    }
    subprocess.run([*VIRSH, "start", victim], capture_output=True)
    facts["wholeAgainSeconds"] = round(whole_again(), 1)
    print(f"[{name}]", json.dumps({kind: facts[kind] for kind in samples}), "session kept:", facts["sessionStillSignedIn"], flush=True)
    return facts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--observe", type=float, default=150)
    ap.add_argument("--only", choices=["both"], help="run one scenario; 'both' first moves the VIP onto a replica's machine")
    args = ap.parse_args()
    started = dt.datetime.now(dt.timezone.utc)
    facts: dict = {"scenario": "aa-phase2", "started": started.isoformat(timespec="seconds"), "vip": VIP,
                   "probeIntervalSeconds": 0.5, "requestTimeoutSeconds": 2}
    whole_again()
    if args.only == "both":
        # The VIP goes where kube-vip's election puts it; restart the holder's kube-vip until it
        # lands on a machine running a replica, then let things settle.
        for _ in range(10):
            w = where()
            if any(n.startswith("jenkins-") for n in w["pods"].get(w["vipHolder"], [])):
                break
            pod = k("-n", "kube-system", "get", "pods", "-l", "app=kube-vip", "--field-selector",
                    f"spec.nodeName={w['vipHolder']}", "-o", "jsonpath={.items[0].metadata.name}")
            k("-n", "kube-system", "delete", "pod", pod)
            time.sleep(20)
        whole_again()
        session, pinned = None, None
        for _ in range(30):
            session = Session()
            pinned = session.sign_in()
            if pinned in ("r0", "r1") and replica_node(pinned) == where()["vipHolder"]:
                break
        else:
            raise SystemExit(f"could not put the VIP and a session's replica on one machine: {where()}")
        facts["both"] = scenario("both", replica_node(pinned), session, pinned, args.observe)
        out = p0.EVIDENCE / f"aa-phase2-both-{started.strftime('%Y%m%dT%H%M%SZ')}.json"
        out.write_text(json.dumps(facts, indent=2) + "\n")
        print("evidence:", out.name)
        return 0

    # The session is signed in before the loss and kept through it: signing in again inside a
    # scenario pinned it anew, to whichever replica answered (the first run measured the survivor).
    def pinned_where(want) -> tuple[Session, str]:
        for _ in range(30):
            session = Session()
            pinned = session.sign_in()
            if pinned in ("r0", "r1") and want(replica_node(pinned), where()["vipHolder"]):
                return session, pinned
        raise RuntimeError("could not pin a session where the scenario needs it")

    # replica: the machine of the session's replica, which does not hold the VIP.
    session, pinned = pinned_where(lambda node, vip: node != vip)
    facts["replica"] = scenario("replica", replica_node(pinned), session, pinned, args.observe)
    # vip: the VIP's machine, which runs no replica the session is on.
    session, pinned = pinned_where(lambda node, vip: node != vip)
    facts["vip"] = scenario("vip", where()["vipHolder"], session, pinned, args.observe)
    # both: the VIP's machine when it also runs the session's replica -- the worst single loss.
    try:
        session, pinned = pinned_where(lambda node, vip: node == vip)
        facts["both"] = scenario("both", replica_node(pinned), session, pinned, args.observe)
    except RuntimeError as exc:
        facts["both"] = {"skipped": f"the VIP's machine runs no replica now ({exc})", "placement": where()}

    out = p0.EVIDENCE / f"aa-phase2-{started.strftime('%Y%m%dT%H%M%SZ')}.json"
    out.write_text(json.dumps(facts, indent=2) + "\n")
    print("evidence:", out.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
