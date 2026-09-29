#!/usr/bin/env python3
"""Write the files the Jenkins controller image is built from, out of toolchain/versions.yaml.

versions.yaml is where netCI decides the controller's base image and every plugin version
(ADR-059); jenkins/plugins.txt and the Dockerfile's base are copies of that decision, kept
identical the way backend/schema.sql is kept identical to the migrations.

    python scripts/toolchain_sync.py           # rewrite jenkins/plugins.txt and the Dockerfile's base
    python scripts/toolchain_sync.py --check   # fail if either has drifted
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
VERSIONS = ROOT / "toolchain" / "versions.yaml"
PLUGINS_TXT = ROOT / "jenkins" / "plugins.txt"
DOCKERFILE = ROOT / "jenkins" / "Dockerfile.controller"
BASE_ARG = re.compile(r"^ARG JENKINS_CONTROLLER_IMAGE=.*$", re.M)

HEADER = (
    "# Generated from toolchain/versions.yaml by scripts/toolchain_sync.py -- edit that file.\n"
    "# Every plugin, dependencies included, at the exact version netCI declares (ADR-059).\n"
)


def declared_jenkins() -> dict:
    return yaml.safe_load(VERSIONS.read_text(encoding="utf-8"))["jenkins"]


def render_plugins(jenkins: dict) -> str:
    return HEADER + "".join(f"{name}:{version}\n" for name, version in sorted(jenkins["plugins"].items()))


def render_dockerfile(jenkins: dict, current: str) -> str:
    if not BASE_ARG.search(current):
        raise SystemExit(f"{DOCKERFILE} has no `ARG JENKINS_CONTROLLER_IMAGE=` line")
    return BASE_ARG.sub(f"ARG JENKINS_CONTROLLER_IMAGE={jenkins['controller']['base']}", current, count=1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    jenkins = declared_jenkins()
    missing = [name for name in jenkins.get("requires", []) if name not in jenkins["plugins"]]
    if missing:
        print(f"required plugins without a pinned version: {', '.join(missing)}", file=sys.stderr)
        return 1
    wanted = {
        PLUGINS_TXT: render_plugins(jenkins),
        DOCKERFILE: render_dockerfile(jenkins, DOCKERFILE.read_text(encoding="utf-8")),
    }
    stale = [path for path, text in wanted.items() if not path.exists() or path.read_text(encoding="utf-8") != text]
    if args.check:
        for path in stale:
            print(f"{path.relative_to(ROOT)} differs from toolchain/versions.yaml; run scripts/toolchain_sync.py", file=sys.stderr)
        return 1 if stale else 0
    for path in stale:
        path.write_text(wanted[path], encoding="utf-8")
        print(f"wrote {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
