#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

require_command cosign
require_file "${ARTIFACT_PATH}"
if [[ -z "${COSIGN_KEY_REF:-}" ]]; then
  echo "COSIGN_KEY_REF is required; refusing unsigned publish" >&2
  exit 1
fi
cosign sign-blob \
  --yes \
  --key "${COSIGN_KEY_REF}" \
  --bundle "${NETCI_OUTPUT_DIR}/signature.bundle.json" \
  "${ARTIFACT_PATH}"
require_file "${NETCI_OUTPUT_DIR}/signature.bundle.json"
