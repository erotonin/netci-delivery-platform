#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

require_command syft
require_file "${OCI_ARCHIVE}"
syft "oci-archive:${OCI_ARCHIVE}" \
  --output "cyclonedx-json=${NETCI_OUTPUT_DIR}/sbom.json"
require_file "${NETCI_OUTPUT_DIR}/sbom.json"
