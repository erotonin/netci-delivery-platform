#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

require_command cosign
require_file "${ARTIFACT_PATH}"
if [[ -z "${COSIGN_KEY_REF:-}" ]]; then
  echo "COSIGN_KEY_REF is required; refusing unsigned publish" >&2
  exit 1
fi
cosign_args=(sign-blob --yes --key "${COSIGN_KEY_REF}" \
  --bundle "${NETCI_OUTPUT_DIR}/signature.bundle.json")
if [[ "${COSIGN_TLOG_UPLOAD:-false}" == "false" ]]; then
  cosign_args+=(--tlog-upload=false)
  # cosign 3.x loads a signing config by default and then refuses --tlog-upload=false;
  # cosign 2.x has no such flag. Detect it rather than pinning to one major version.
  if cosign sign-blob --help 2>&1 | grep -q -- '--use-signing-config'; then
    cosign_args+=(--use-signing-config=false)
  fi
fi

cosign "${cosign_args[@]}" "${ARTIFACT_PATH}"
require_file "${NETCI_OUTPUT_DIR}/signature.bundle.json"
