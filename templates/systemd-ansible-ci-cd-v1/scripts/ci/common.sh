#!/usr/bin/env bash
set -euo pipefail

ci_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${ci_dir}/../../../.." && pwd)"
NETCI_APP_DIR="${NETCI_APP_DIR:-${repo_root}/sample-apps/hello-systemd-go}"
NETCI_OUTPUT_DIR="${NETCI_OUTPUT_DIR:-${repo_root}}"
# The version baked into the binary is the commit it was built from (Jenkins passes
# COMMIT_SHA); the health gate compares it, so a stale process cannot pass for the new one.
VERSION="${VERSION:-${COMMIT_SHA:-${GIT_COMMIT:-v0.1.0}}}"

if [[ ! "${VERSION}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]]; then
  echo "VERSION must be a safe immutable release identifier" >&2
  exit 1
fi

ARTIFACT_PATH="${ARTIFACT_PATH:-${NETCI_APP_DIR}/dist/hello-systemd-${VERSION}}"
# The repository in the registry the binary is pushed to as an OCI artifact.
NETCI_ARTIFACT_NAME="${NETCI_ARTIFACT_NAME:-hello-systemd-go}"
REGISTRY_PUSH_HOST="${REGISTRY_PUSH_HOST:-localhost:5000}"
REGISTRY_PULL_HOST="${REGISTRY_PULL_HOST:-${REGISTRY_PUSH_HOST}}"
mkdir -p "${NETCI_OUTPUT_DIR}"

require_command() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "required command not found: $1" >&2
    exit 1
  }
}

require_file() {
  [[ -s "$1" ]] || {
    echo "required output is missing or empty: $1" >&2
    exit 1
  }
}
