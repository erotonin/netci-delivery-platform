#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

require_command trivy

# Scan the image in the registry, by digest, rather than a local archive. Two reasons:
# it is exactly the artifact that will be deployed, and Trivy cannot read the OCI
# archive format buildah writes. push-image.sh is idempotent, so calling it here does
# not re-upload on the later sign/publish stages.
bash "$(dirname "${BASH_SOURCE[0]}")/push-image.sh"
registry_artifact_ref="$(tr -d '\r\n' < "${NETCI_OUTPUT_DIR}/registry-artifact-ref.txt")"

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
if [[ "${REGISTRY_TLS_VERIFY}" == "false" ]]; then
  trivy_flags+=(--insecure)
fi

trivy image \
  --exit-code 1 \
  "${trivy_flags[@]}" \
  --output "${NETCI_OUTPUT_DIR}/scan-report.json" \
  "${registry_artifact_ref}"
require_file "${NETCI_OUTPUT_DIR}/scan-report.json"
