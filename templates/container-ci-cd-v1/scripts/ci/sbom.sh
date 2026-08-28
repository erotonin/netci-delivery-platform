#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

require_command syft

# The SBOM describes the pushed digest, so the SBOM, the scan report and the signature
# all refer to the same immutable artifact rather than to three local copies of it.
bash "$(dirname "${BASH_SOURCE[0]}")/push-image.sh"
registry_artifact_ref="$(tr -d '\r\n' < "${NETCI_OUTPUT_DIR}/registry-artifact-ref.txt")"

if [[ "${REGISTRY_TLS_VERIFY}" == "false" ]]; then
  export SYFT_REGISTRY_INSECURE_USE_HTTP=true
  export SYFT_REGISTRY_INSECURE_SKIP_TLS_VERIFY=true
fi

syft "registry:${registry_artifact_ref}" \
  --output "cyclonedx-json=${NETCI_OUTPUT_DIR}/sbom.json"
require_file "${NETCI_OUTPUT_DIR}/sbom.json"
