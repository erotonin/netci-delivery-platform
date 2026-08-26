#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

require_command trivy
require_file "${ARTIFACT_PATH}"
trivy rootfs \
  --exit-code 1 \
  --severity HIGH,CRITICAL \
  --format json \
  --output "${NETCI_OUTPUT_DIR}/scan-report.json" \
  "${ARTIFACT_PATH}"
require_file "${NETCI_OUTPUT_DIR}/scan-report.json"
