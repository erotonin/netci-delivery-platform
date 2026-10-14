#!/usr/bin/env python3
"""The Secrets the active-active Jenkins chart names but never creates (deploy/helm/netci-jenkins-ha,
docs/JENKINS-HA-PRODUCTION.md). Nothing secret is printed, put on a command line, or left on disk.

    python3 scripts/jenkins_ha_secrets.py generate  -n NS --replicas 2
        homesync's replicas file and each replica's token (homesync-replicas, homesync-tokens) and
        the shared sign-in key (jenkins-session-key). Refuses to replace one that exists: a new
        homesync token locks a running replica out, a new session key signs everyone out.
        --rotate replaces them anyway.
    python3 scripts/jenkins_ha_secrets.py seed-home -n NS [--secret jenkins-home-secrets]
        jenkins-0's secrets/ and identity.key.enc as the Secret every replica copies into its home at
        start (the chart's init container). Run it with one replica, before the second starts:
        credentials, agents' secrets and the instance identity are keyed by these files, and two
        replicas that made their own are two Jenkins to their agents (ADR-076 phase 5).

Secret names are the defaults of ci/production-values.yaml; --*-secret options change them.
"""
from __future__ import annotations

import argparse
import io
import json
import secrets
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path


def kubectl(*args: str, stdin: bytes | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["kubectl", *args], input=stdin, capture_output=True, check=check)


def exists(ns: str, name: str) -> bool:
    return kubectl("-n", ns, "get", "secret", name, check=False).returncode == 0


def apply_secret(ns: str, name: str, files: dict[str, bytes]) -> None:
    """A generic Secret from files written to a private directory, never from literals: a literal
    is on the command line, where any process listing can read it."""
    with tempfile.TemporaryDirectory(prefix="netci-secret-") as d:
        args = ["-n", ns, "create", "secret", "generic", name, "--dry-run=client", "-o", "yaml"]
        for key, value in files.items():
            p = Path(d) / key
            p.write_bytes(value)
            p.chmod(0o600)
            args.append(f"--from-file={key}={p}")
        manifest = kubectl(*args).stdout
    kubectl("apply", "-f", "-", stdin=manifest)


def generate(a: argparse.Namespace) -> int:
    names = [a.replicas_secret, a.tokens_secret, a.session_secret]
    present = [n for n in names if exists(a.namespace, n)]
    if present and not a.rotate:
        print(f"already there: {', '.join(present)} -- not replaced (--rotate replaces them; see the docstring)", file=sys.stderr)
        return 1
    tokens = {f"jenkins-{i}": secrets.token_urlsafe(32) for i in range(a.replicas)}
    replicas = "".join(f"{pod}:{token}\n" for pod, token in tokens.items())
    apply_secret(a.namespace, a.replicas_secret, {"replicas": replicas.encode()})
    apply_secret(a.namespace, a.tokens_secret, {pod: token.encode() for pod, token in tokens.items()})
    apply_secret(a.namespace, a.session_secret, {"key": secrets.token_urlsafe(48).encode()})
    print(f"made {', '.join(names)} in {a.namespace} for {a.replicas} replicas")
    if present:
        print("rotated: restart homesync and the replicas (lab/aa/chart.sh or helm upgrade with a changed annotation)")
    return 0


# Under JENKINS_HOME: the keys of credentials (hudson.util.Secret, master.key), of the agents'
# secrets (JnlpSlaveAgentProtocol), of crumbs and cookies, and the instance identity.
HOME_FILES = ["secrets", "identity.key.enc"]


def seed_home(a: argparse.Namespace) -> int:
    pods = json.loads(kubectl("-n", a.namespace, "get", "pods", "-l", "app=jenkins", "-o", "json").stdout)["items"]
    running = sorted(p["metadata"]["name"] for p in pods if p["status"].get("phase") == "Running")
    if running != ["jenkins-0"] and not a.force:
        print(f"replicas running: {running}. Seed with jenkins-0 alone (replicas: 1), before a second has made its own keys;"
              " --force if they are already the same", file=sys.stderr)
        return 1
    agent_key = "secrets/jenkins.slaves.JnlpSlaveAgentProtocol.secret"
    if kubectl("-n", a.namespace, "exec", "jenkins-0", "-c", "jenkins", "--", "test", "-s", f"/var/jenkins_home/{agent_key}",
               check=False).returncode != 0:
        print(f"{agent_key} is not made yet: Jenkins makes it the first time an agent's secret is computed --"
              " open one inbound agent's page on jenkins-0 once, then run this again", file=sys.stderr)
        return 1
    raw = kubectl("-n", a.namespace, "exec", "jenkins-0", "-c", "jenkins", "--", "tar", "-C", "/var/jenkins_home", "-cf", "-",
                  *HOME_FILES).stdout
    files: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(raw)) as t:
        for m in t.getmembers():
            if not m.isfile():
                continue
            name = Path(m.name).name
            # The init container puts identity.key.enc in the home and everything else in secrets/;
            # secrets/filepath-filters.d and the like are made by Jenkins and not needed.
            if m.name == "identity.key.enc" or Path(m.name).parent == Path("secrets"):
                f = t.extractfile(m)
                files[name] = f.read() if f else b""
    if "master.key" not in files or "identity.key.enc" not in files:
        print("jenkins-0's home has no master.key or identity.key.enc: is it started?", file=sys.stderr)
        return 1
    apply_secret(a.namespace, a.secret, files)
    print(f"made {a.secret} in {a.namespace} from jenkins-0 ({len(files)} files); now set replicas to the target count")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate")
    g.add_argument("-n", "--namespace", required=True)
    g.add_argument("--replicas", type=int, required=True)
    g.add_argument("--replicas-secret", default="homesync-replicas")
    g.add_argument("--tokens-secret", default="homesync-tokens")
    g.add_argument("--session-secret", default="jenkins-session-key")
    g.add_argument("--rotate", action="store_true")
    s = sub.add_parser("seed-home")
    s.add_argument("-n", "--namespace", required=True)
    s.add_argument("--secret", default="jenkins-home-secrets")
    s.add_argument("--force", action="store_true")
    a = ap.parse_args()
    if a.cmd == "generate" and not 1 <= a.replicas <= 9:
        ap.error("--replicas: 1 to 9")
    return generate(a) if a.cmd == "generate" else seed_home(a)


if __name__ == "__main__":
    sys.exit(main())
