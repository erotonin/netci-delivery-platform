#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

require_command trivy
require_file "${ARTIFACT_PATH}"

# netCI blocks findings that a rebuild can actually fix. Vulnerabilities with no
# published fix are still recorded in the report (and in the evidence netCI stores),
# but they cannot be actioned by re-running CI, so failing every build on them would
# make the gate meaningless rather than strict. Set TRIVY_IGNORE_UNFIXED=false to
# block on unfixed findings as well.
TRIVY_IGNORE_UNFIXED="${TRIVY_IGNORE_UNFIXED:-true}"
trivy_flags=(--severity HIGH,CRITICAL --scanners vuln --format json)
# A mirrored vulnerability database, for build agents with no route to the internet.
if [[ -n "${TRIVY_DB_REPOSITORY:-}" ]]; then
  trivy_flags+=(--db-repository "${TRIVY_DB_REPOSITORY}")
fi
if [[ "${TRIVY_IGNORE_UNFIXED}" == "true" ]]; then
  trivy_flags+=(--ignore-unfixed)
fi

trivy rootfs \
  --exit-code 1 \
  "${trivy_flags[@]}" \
  --output "${NETCI_OUTPUT_DIR}/scan-report.json" \
  "${ARTIFACT_PATH}"
require_file "${NETCI_OUTPUT_DIR}/scan-report.json"
