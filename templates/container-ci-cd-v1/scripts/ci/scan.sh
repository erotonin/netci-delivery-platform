#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

require_command trivy
require_file "${OCI_ARCHIVE}"
trivy image \
  --input "${OCI_ARCHIVE}" \
  --exit-code 1 \
  --severity HIGH,CRITICAL \
  --format json \
  --output "${NETCI_OUTPUT_DIR}/scan-report.json"
require_file "${NETCI_OUTPUT_DIR}/scan-report.json"
