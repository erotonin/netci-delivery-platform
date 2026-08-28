#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

require_command curl
require_command sha256sum
require_file "${ARTIFACT_PATH}"
require_file "${NETCI_OUTPUT_DIR}/sbom.json"
require_file "${NETCI_OUTPUT_DIR}/scan-report.json"
require_file "${NETCI_OUTPUT_DIR}/signature.bundle.json"

if [[ -z "${ARTIFACT_BINARY_UPLOAD_URL:-}" || -z "${ARTIFACT_DOWNLOAD_URL:-}" ]]; then
  echo "ARTIFACT_BINARY_UPLOAD_URL and ARTIFACT_DOWNLOAD_URL are required" >&2
  exit 1
fi

artifact_sha256="$(sha256sum "${ARTIFACT_PATH}" | awk '{print $1}')"
built_digest="$(tr -d '\r\n' < "${NETCI_OUTPUT_DIR}/artifact-digest.txt" 2>/dev/null || true)"
if [[ -n "${built_digest}" && "${built_digest}" != "sha256:${artifact_sha256}" ]]; then
  echo "artifact changed after it was scanned and signed: ${built_digest} != sha256:${artifact_sha256}" >&2
  exit 1
fi
curl --fail --show-error --silent \
  --upload-file "${ARTIFACT_PATH}" \
  "${ARTIFACT_BINARY_UPLOAD_URL}"

# netCI can provide separate pre-signed URLs for evidence. They are optional
# here only because the API may upload archived Jenkins artifacts itself.
for mapping in \
  "${SBOM_UPLOAD_URL:-}|${NETCI_OUTPUT_DIR}/sbom.json" \
  "${SCAN_REPORT_UPLOAD_URL:-}|${NETCI_OUTPUT_DIR}/scan-report.json" \
  "${SIGNATURE_BUNDLE_UPLOAD_URL:-}|${NETCI_OUTPUT_DIR}/signature.bundle.json"; do
  upload_url="${mapping%%|*}"
  upload_file="${mapping#*|}"
  if [[ -n "${upload_url}" ]]; then
    curl --fail --show-error --silent --upload-file "${upload_file}" "${upload_url}"
  fi
done

printf 'sha256:%s\n' "${artifact_sha256}" > "${NETCI_OUTPUT_DIR}/artifact-digest.txt"
printf '%s\n' "${ARTIFACT_DOWNLOAD_URL}" > "${NETCI_OUTPUT_DIR}/artifact-ref.txt"
