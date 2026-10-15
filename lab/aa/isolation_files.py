#!/usr/bin/env python3
"""The files the IDP needs for per-project build namespaces on the HA lab (ADR-096), mode 600:

  .netci-gate/aa/kubeconfigs/build-provisioner   the netci-build-provisioner account (lab/aa/build-isolation.yaml)
  .netci-gate/aa/build-pull.json                 the pull credential written into each build namespace

Run by `lab/aa/idp.sh isolation-prepare`. Nothing in them is printed.
"""
import base64
import json
import os
import subprocess

import yaml


def k(*args: str) -> str:
    return subprocess.run(["kubectl", *args], check=True, capture_output=True, text=True).stdout


def private(path: str, data: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.write(fd, data.encode())
    os.close(fd)


def main() -> None:
    token = json.loads(k("-n", "netci-deploy", "get", "secret", "netci-build-provisioner-token", "-o", "json"))["data"]
    lab = yaml.safe_load(open(os.environ["KUBECONFIG"]))
    private(".netci-gate/aa/kubeconfigs/build-provisioner", yaml.safe_dump({
        "apiVersion": "v1", "kind": "Config", "current-context": "build",
        "clusters": [{"name": "aa-lab", "cluster": {"server": lab["clusters"][0]["cluster"]["server"],
                                                    "certificate-authority-data": token["ca.crt"]}}],
        "users": [{"name": "netci-build-provisioner", "user": {"token": base64.b64decode(token["token"]).decode()}}],
        "contexts": [{"name": "build", "context": {"cluster": "aa-lab", "user": "netci-build-provisioner"}}],
    }))
    pull = json.loads(k("-n", "aa-p2", "get", "secret", "harbor-pull", "-o", "json"))["data"][".dockerconfigjson"]
    private(".netci-gate/aa/build-pull.json", base64.b64decode(pull).decode())
    print("wrote the provisioner's kubeconfig and the build pull secret file (mode 600)")


if __name__ == "__main__":
    main()
