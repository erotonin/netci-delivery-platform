#!/usr/bin/env bash
set -euo pipefail

ci_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${ci_dir}/../../../.." && pwd)"
export NETCI_APP_DIR="${NETCI_APP_DIR:-${repo_root}/sample-apps/hello-kubernetes}"
export NETCI_IMAGE_NAME="${NETCI_IMAGE_NAME:-hello-kubernetes}"
export DEPLOY_IMAGE_REPOSITORY="${DEPLOY_IMAGE_REPOSITORY:-${REGISTRY_PULL_HOST:-localhost:5000}/${NETCI_IMAGE_NAMESPACE:+${NETCI_IMAGE_NAMESPACE}/}${NETCI_IMAGE_NAME}}"
container_ci_dir="${repo_root}/templates/container-ci-cd-v1/scripts/ci"
