#!/usr/bin/env bash
set -euo pipefail

ci_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${ci_dir}/../../../.." && pwd)"

NETCI_APP_DIR="${NETCI_APP_DIR:-${repo_root}/sample-apps/hello-container}"
NETCI_IMAGE_NAME="${NETCI_IMAGE_NAME:-hello-container}"
NETCI_OUTPUT_DIR="${NETCI_OUTPUT_DIR:-${repo_root}}"
IMAGE_TAG="${IMAGE_TAG:-${GIT_COMMIT:-local}}"
REGISTRY_PUSH_HOST="${REGISTRY_PUSH_HOST:-netci-registry:5000}"
REGISTRY_PULL_HOST="${REGISTRY_PULL_HOST:-localhost:5000}"
REGISTRY_TLS_VERIFY="${REGISTRY_TLS_VERIFY:-false}"
PUSH_IMAGE_REPOSITORY="${PUSH_IMAGE_REPOSITORY:-${REGISTRY_PUSH_HOST}/${NETCI_IMAGE_NAME}}"
DEPLOY_IMAGE_REPOSITORY="${DEPLOY_IMAGE_REPOSITORY:-${REGISTRY_PULL_HOST}/${NETCI_IMAGE_NAME}}"
PUSH_IMAGE_REF="${PUSH_IMAGE_REF:-${PUSH_IMAGE_REPOSITORY}:${IMAGE_TAG}}"
OCI_ARCHIVE="${OCI_ARCHIVE:-${NETCI_OUTPUT_DIR}/${NETCI_IMAGE_NAME}.oci.tar}"
# A build agent usually cannot reach Docker Hub, and should not need to: the base image
# belongs in the same registry as everything else the pipeline consumes. Empty means
# "use whatever the Dockerfile declares".
NETCI_BASE_IMAGE="${NETCI_BASE_IMAGE:-}"

if [[ ! "${IMAGE_TAG}" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$ ]]; then
  echo "IMAGE_TAG must be an OCI-compatible immutable build identifier" >&2
  exit 1
fi
if [[ "${REGISTRY_TLS_VERIFY}" != "true" && "${REGISTRY_TLS_VERIFY}" != "false" ]]; then
  echo "REGISTRY_TLS_VERIFY must be true or false" >&2
  exit 1
fi
# Cosign talks to the same registry, so it is insecure exactly when buildah is. It used to
# default to --allow-insecure-registry whatever REGISTRY_TLS_VERIFY said. The defaults of
# a run nothing configured (false / insecure / no tlog) are unchanged; netCI sends
# REGISTRY_TLS_VERIFY and COSIGN_TLOG_UPLOAD with every build it dispatches (ADR-054).
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
# "false" for a verify-only build -- a fork's pull request (ADR-043): it is tested, built
# and scanned from the local archive, and nothing it produced may reach the registry.
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
