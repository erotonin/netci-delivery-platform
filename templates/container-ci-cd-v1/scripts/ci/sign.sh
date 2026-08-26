#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

require_command cosign
if [[ -z "${COSIGN_KEY_REF:-}" ]]; then
  echo "COSIGN_KEY_REF is required; refusing unsigned publish" >&2
  exit 1
fi

bash "$(dirname "${BASH_SOURCE[0]}")/push-image.sh"
registry_artifact_ref="$(tr -d '\r\n' < "${NETCI_OUTPUT_DIR}/registry-artifact-ref.txt")"
cosign_args=(sign --yes --key "${COSIGN_KEY_REF}" --bundle "${NETCI_OUTPUT_DIR}/signature.bundle.json")
if [[ "${COSIGN_ALLOW_INSECURE_REGISTRY:-true}" == "true" ]]; then
  cosign_args+=(--allow-insecure-registry)
fi
if [[ "${COSIGN_TLOG_UPLOAD:-false}" == "false" ]]; then
  cosign_args+=(--tlog-upload=false)
fi
cosign "${cosign_args[@]}" "${registry_artifact_ref}"
require_file "${NETCI_OUTPUT_DIR}/signature.bundle.json"
