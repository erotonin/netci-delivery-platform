#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
toolbox_image="${JENKINS_TOOLBOX_IMAGE:-netci/ci-toolbox:0.1.0}"

docker build \
  --pull \
  --tag "${toolbox_image}" \
  "${repo_root}/jenkins/agent-toolbox"

printf 'built %s\n' "${toolbox_image}"
