#!/usr/bin/env python3
"""Write .netci-gate/aa/idp.env: the IDP's profile against the active-active replicas (ADR-091).

From the systemd lab's profile (.netci-gate/real-local.env) it keeps sign-in, separation of duties,
evidence and signature policy, the workload token keys and the Ansible keys; CI goes to the replicas
through netci-queue, artifacts to Harbor, CD to the HA lab's own Temporal, state to its own database.
The file is written with mode 600 and nothing in it is printed.
"""
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
KEEP = re.compile(
    r"^(NETCI_(AUTH_MODE|OIDC_[A-Z_]+|REQUIRE_SEPARATION_OF_DUTIES|ALLOW_DEVELOPER_PROD_CD|"
    r"REQUIRE_SECURITY_EVIDENCE|SIGNATURE_[A-Z_]+|COSIGN_[A-Z_]+|WORKLOAD_TOKEN_KEYS_FILE|ANSIBLE_[A-Z_]+|"
    r"PREFLIGHT_DISK_THRESHOLD|BACKUP_ENCRYPTION_KEY|LAB_USER_PASSWORD)|NETCI_ACCEPTANCE_[A-Z_]+)=")


def main() -> None:
    lines = [line for line in (ROOT / ".netci-gate/real-local.env").read_text().splitlines()
             if KEEP.match(line.removeprefix("export "))]
    queue = ROOT / ".netci-gate/aa/queue"

    def read(name: str) -> str:
        return (queue / name).read_text().strip()

    ha = {
        "NETCI_ENVIRONMENT": "production",
        "DATABASE_URL": "postgresql://netci:netci-local-only@127.0.0.1:55434/netci_ha",
        "NETCI_API_URL": "http://127.0.0.1:18100",
        "NETCI_ALLOWED_ORIGINS": "http://localhost:5173,http://127.0.0.1:5173",
        # Build pods call back from the lab network, where the host is 192.168.122.1.
        "NETCI_CALLBACK_URL": "http://192.168.122.1:18100",
        "NETCI_CI_MODE": "jenkins",
        "NETCI_JENKINS_CONTROLLERS": "HA",
        "JENKINS_HA_ID": "jenkins-ha",
        "JENKINS_HA_URL": "http://192.168.122.240",
        "JENKINS_HA_USERNAME": read("jenkins-user"),
        "JENKINS_HA_API_TOKEN": read("jenkins-token"),
        "NETCI_RUN_QUEUE_URL": "http://192.168.122.212:30091",
        "NETCI_RUN_QUEUE_TOKEN_FILE": str(queue / "client-token"),
        "NETCI_GIT_CREDENTIALS_ID": "netci-git",
        # The API's own read token for branch and commit lists (git credential store).
        "NETCI_GIT_CREDENTIALS_FILE": str(ROOT / ".netci-gate/aa/git-credentials"),
        "NETCI_REGISTRY_CREDENTIALS_ID": "netci-harbor",
        "NETCI_COSIGN_CREDENTIALS_ID": "netci-cosign-key",
        "NETCI_REGISTRY_PUSH_HOST": "172.17.0.1:8930",
        "NETCI_REGISTRY_PULL_HOST": "172.17.0.1:8930",
        "NETCI_REGISTRY_NAMESPACE": "apps",
        "NETCI_REGISTRY_ALLOW_HTTP": "false",
        "NETCI_BUILD_BASE_IMAGE": "172.17.0.1:8930/mirror/netci/python-base:3.12-alpine",
        "NETCI_TRIVY_DB_REPOSITORY": "172.17.0.1:8930/mirror/aquasec/trivy-db:2",
        "NETCI_BUILD_ISOLATION": "none",
        # cosign, run by the worker to verify a digest before deploying it, must trust the
        # registry's CA; without it the TLS attempt failed and go-containerregistry fell back to
        # plain HTTP on an RFC 1918 address ("plain HTTP request was sent to HTTPS port").
        "SSL_CERT_DIR": f"/etc/ssl/certs:{ROOT / '.netci-gate/aa/registry-ca'}",
        # The scan's Trivy DB is mirrored on a timer (netci-trivy-db-mirror), so a stale one stops
        # the artifact rather than being let through with a warning.
        "NETCI_TOOLCHAIN_ENFORCE": "enforce",
        "NETCI_CD_MODE": "temporal",
        # kubeconfigRef "aa-lab" (lab/aa/idp.sh deploy-prepare): netci-deployer, three namespaces.
        "NETCI_KUBECONFIG_DIR": str(ROOT / ".netci-gate/aa/kubeconfigs"),
        "TEMPORAL_ADDRESS": "127.0.0.1:7233",
        "TEMPORAL_NAMESPACE": "default",
        "TEMPORAL_TASK_QUEUE": "netci-ha-delivery",
    }
    out = ROOT / ".netci-gate/aa/idp.env"
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.write(fd, ("\n".join(lines + [f"{k}={v}" for k, v in ha.items()]) + "\n").encode())
    os.close(fd)
    print(f"wrote {out.relative_to(ROOT)}: {len(lines)} settings kept, {len(ha)} for the HA lab")


if __name__ == "__main__":
    main()
