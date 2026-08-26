#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

require_command buildah
require_file "${OCI_ARCHIVE}"

digest_file="${NETCI_OUTPUT_DIR}/artifact-digest.txt"
registry_ref_file="${NETCI_OUTPUT_DIR}/registry-artifact-ref.txt"
deploy_ref_file="${NETCI_OUTPUT_DIR}/artifact-ref.txt"

if [[ -s "${digest_file}" && -s "${registry_ref_file}" && -s "${deploy_ref_file}" ]]; then
  exit 0
fi

temporary_digest="${digest_file}.tmp"
rm -f "${temporary_digest}"
buildah push \
  --tls-verify="${REGISTRY_TLS_VERIFY}" \
  --digestfile "${temporary_digest}" \
  "${PUSH_IMAGE_REF}" \
  "docker://${PUSH_IMAGE_REF}"

artifact_digest="$(tr -d '\r\n' < "${temporary_digest}")"
if [[ ! "${artifact_digest}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  echo "registry returned an invalid digest: ${artifact_digest}" >&2
  exit 1
fi
mv "${temporary_digest}" "${digest_file}"
printf '%s@%s\n' "${PUSH_IMAGE_REPOSITORY}" "${artifact_digest}" > "${registry_ref_file}"
printf '%s@%s\n' "${DEPLOY_IMAGE_REPOSITORY}" "${artifact_digest}" > "${deploy_ref_file}"
