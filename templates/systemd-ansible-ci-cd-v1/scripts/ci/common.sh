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
# sign.sh uploads and signs with cosign, which is insecure exactly when the registry is
# (ADR-054). Unset, both keep their old defaults: TLS off, --allow-insecure-registry,
# no tlog upload. netCI sends REGISTRY_TLS_VERIFY and COSIGN_TLOG_UPLOAD with every build.
REGISTRY_TLS_VERIFY="${REGISTRY_TLS_VERIFY:-false}"
if [[ "${REGISTRY_TLS_VERIFY}" != "true" && "${REGISTRY_TLS_VERIFY}" != "false" ]]; then
  echo "REGISTRY_TLS_VERIFY must be true or false" >&2
  exit 1
fi
if [[ -z "${COSIGN_ALLOW_INSECURE_REGISTRY:-}" ]]; then
  if [[ "${REGISTRY_TLS_VERIFY}" == "false" ]]; then
    COSIGN_ALLOW_INSECURE_REGISTRY=true
  else
    COSIGN_ALLOW_INSECURE_REGISTRY=false
  fi
fi
if [[ "${COSIGN_ALLOW_INSECURE_REGISTRY}" == "true" && "${REGISTRY_TLS_VERIFY}" == "true" ]]; then
  echo "COSIGN_ALLOW_INSECURE_REGISTRY=true contradicts REGISTRY_TLS_VERIFY=true; refusing to sign insecurely" >&2
  exit 1
fi
COSIGN_TLOG_UPLOAD="${COSIGN_TLOG_UPLOAD:-false}"
if [[ "${COSIGN_TLOG_UPLOAD}" != "true" && "${COSIGN_TLOG_UPLOAD}" != "false" ]]; then
  echo "COSIGN_TLOG_UPLOAD must be true or false" >&2
  exit 1
fi
# "false" for a verify-only build (a fork's pull request, ADR-043): nothing it built may
# reach the registry. Only sign.sh uploads, and it refuses such a build.
NETCI_PUBLISH="${NETCI_PUBLISH:-true}"
if [[ "${NETCI_PUBLISH}" != "true" && "${NETCI_PUBLISH}" != "false" ]]; then
  echo "NETCI_PUBLISH must be true or false" >&2
  exit 1
fi
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
