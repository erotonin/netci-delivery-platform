#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

# Signing pushes to a quarantine reference first because Cosign signs a remote
# OCI digest. Publish only exposes metadata after all required evidence exists.
bash "$(dirname "${BASH_SOURCE[0]}")/push-image.sh"
require_file "${NETCI_OUTPUT_DIR}/artifact-digest.txt"
require_file "${NETCI_OUTPUT_DIR}/artifact-ref.txt"
require_file "${NETCI_OUTPUT_DIR}/sbom.json"
require_file "${NETCI_OUTPUT_DIR}/scan-report.json"
require_file "${NETCI_OUTPUT_DIR}/signature.bundle.json"
printf 'published %s\n' "$(tr -d '\r\n' < "${NETCI_OUTPUT_DIR}/artifact-ref.txt")"
