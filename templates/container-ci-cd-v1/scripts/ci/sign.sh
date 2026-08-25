#!/usr/bin/env bash
set -euo pipefail
IMAGE_REF="${IMAGE_REF:-registry.localhost:5000/hello-container:${GIT_COMMIT:-local}}"
if [[ -z "${COSIGN_KEY_REF:-}" ]]; then
  echo "COSIGN_KEY_REF is required; refusing unsigned publish" >&2
  exit 1
fi
cosign sign --key "${COSIGN_KEY_REF}" "${IMAGE_REF}"
