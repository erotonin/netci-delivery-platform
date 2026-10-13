#!/usr/bin/env python3
"""Give both replicas one secrets/ set, and the agents their secrets (ADR-076 phase 5).

    python3 lab/aa/phase5_secrets.py

1. jenkins-0's secrets/ (with identity.key.enc), after it has computed the agents' secrets once so
   the key behind them exists, becomes the Secret home-secrets; phase3.yaml's init container copies
   it into every replica's home at start.
2. Both replicas restarted; the agents' secrets read from each replica must be equal -- checked.
3. They become the Secret agent-secrets the agent pods read. Nothing secret is printed or written
   outside .netci-gate.
"""
from __future__ import annotations

import io
import json
import subprocess
import sys
import tarfile
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import phase3_probe as p3  # noqa: E402

NS = p3.NS
OUT = p3.p0.STATE / "aa-secrets"
AGENTS = ["agent-0", "agent-1"]
SCRIPT = "print(jenkins.slaves.JnlpSlaveAgentProtocol.SLAVE_SECRET.mac('%s'))"


def agent_secret(c: p3.R, name: str) -> str:
    """The agent's secret as this very replica computes it. The balancer sends a request pinned to a
    replica that is not ready to the other one, so an answer from the wrong replica is retried, not
    believed -- the first version compared r1 with itself and called the replicas equal."""
    for _ in range(60):
        # A fresh client each time: when the balancer redispatches a pinned request it re-pins the
        # cookie to the replica that answered, and an old client would stay on it for good.
        fresh = p3.R(c.replica)
        code, body, served = fresh.req("/scriptText", urllib.parse.urlencode({"script": SCRIPT % name}).encode(),
                                       "application/x-www-form-urlencoded")
        if code == 200 and body.strip() and served == c.replica:
            return body.strip()
        time.sleep(2)
    raise SystemExit(f"{c.replica} did not answer for itself")


def k(*args: str, stdin: bytes | None = None) -> bytes:
    env = {"KUBECONFIG": str(p3.p0.STATE / "kubeconfig"), "PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(Path.home())}
    return subprocess.run(["kubectl", *args], input=stdin, capture_output=True, check=True, env=env).stdout


def restart_replicas() -> None:
    k("-n", NS, "rollout", "restart", "statefulset/jenkins")
    k("-n", NS, "rollout", "status", "statefulset/jenkins", "--timeout=900s")
    p3.until(lambda: p3.R("r0").req("/api/json")[0] == 200 and p3.R("r1").req("/api/json")[0] == 200, 300, 2)


def main() -> int:
    OUT.mkdir(mode=0o700, exist_ok=True)
    for old in OUT.iterdir():
        old.unlink()
    for name in AGENTS:  # makes jenkins-0 create the key behind the agents' secrets
        agent_secret(p3.R("r0"), name)
    # The key behind the agents' secrets is written when it is first used; it must be in the copy.
    k("-n", NS, "exec", "jenkins-0", "-c", "jenkins", "--", "test", "-s",
      "/var/jenkins_home/secrets/jenkins.slaves.JnlpSlaveAgentProtocol.secret")
    raw = k("-n", NS, "exec", "jenkins-0", "-c", "jenkins", "--", "tar", "-C", "/var/jenkins_home", "-cf", "-",
            "secrets", "identity.key.enc")
    files: list[str] = []
    with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
        for m in tar.getmembers():
            if m.isfile() and "/" not in m.name.removeprefix("secrets/"):
                target = OUT / Path(m.name).name
                target.write_bytes(tar.extractfile(m).read())
                target.chmod(0o600)
                files.append(str(target))
    args = ["-n", NS, "create", "secret", "generic", "home-secrets", "--dry-run=client", "-o", "yaml"]
    args += [f"--from-file={f}" for f in files]
    k("apply", "-f", "-", stdin=k(*args))
    print(f"home-secrets: {len(files)} files from jenkins-0", flush=True)

    restart_replicas()
    secrets = {}
    for name in AGENTS:
        s0, s1 = agent_secret(p3.R("r0"), name), agent_secret(p3.R("r1"), name)
        if s0 != s1:
            raise SystemExit(f"{name}: the replicas still disagree on its secret")
        secrets[name] = s0
    for name, value in secrets.items():
        (OUT / f"agent-{name}").write_text(value)
        (OUT / f"agent-{name}").chmod(0o600)
    args = ["-n", NS, "create", "secret", "generic", "agent-secrets", "--dry-run=client", "-o", "yaml"]
    args += [f"--from-file={name}={OUT / f'agent-{name}'}" for name in AGENTS]
    k("apply", "-f", "-", stdin=k(*args))
    print(json.dumps({"agents": AGENTS, "sameSecretOnBothReplicas": True}), flush=True)
    time.sleep(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
