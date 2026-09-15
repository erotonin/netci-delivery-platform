#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

# The artifact was pushed to the registry and signed in sign.sh (cosign signs a remote
# digest, so push and sign are one step). Publishing here means checking that every
# piece of evidence the callback will submit exists and agrees with itself.
require_command sha256sum
require_file "${ARTIFACT_PATH}"
require_file "${NETCI_OUTPUT_DIR}/sbom.json"
require_file "${NETCI_OUTPUT_DIR}/scan-report.json"
require_file "${NETCI_OUTPUT_DIR}/signature.bundle.json"
require_file "${NETCI_OUTPUT_DIR}/artifact-digest.txt"
require_file "${NETCI_OUTPUT_DIR}/artifact-ref.txt"
require_file "${NETCI_OUTPUT_DIR}/artifact-blob-sha256.txt"

blob_sha256="$(sha256sum "${ARTIFACT_PATH}" | awk '{print $1}')"
recorded_blob="$(tr -d '\r\n' < "${NETCI_OUTPUT_DIR}/artifact-blob-sha256.txt")"
if [[ "${blob_sha256}" != "${recorded_blob}" ]]; then
  echo "artifact changed after it was pushed and signed: ${recorded_blob} != ${blob_sha256}" >&2
  exit 1
fi
artifact_digest="$(tr -d '\r\n' < "${NETCI_OUTPUT_DIR}/artifact-digest.txt")"
artifact_ref="$(tr -d '\r\n' < "${NETCI_OUTPUT_DIR}/artifact-ref.txt")"
if [[ "${artifact_ref}" != *"@${artifact_digest}" ]]; then
  echo "artifact-ref.txt (${artifact_ref}) does not name artifact-digest.txt (${artifact_digest})" >&2
  exit 1
fi
echo "published ${artifact_ref} (blob sha256 ${blob_sha256})"
