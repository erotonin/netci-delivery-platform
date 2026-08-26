#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

require_command buildah
require_file "${NETCI_APP_DIR}/Dockerfile"

buildah bud \
  --isolation "${BUILDAH_ISOLATION:-chroot}" \
  --layers=false \
  --tag "${PUSH_IMAGE_REF}" \
  "${NETCI_APP_DIR}"
rm -f "${OCI_ARCHIVE}"
buildah push "${PUSH_IMAGE_REF}" "oci-archive:${OCI_ARCHIVE}:${NETCI_IMAGE_NAME}"
require_file "${OCI_ARCHIVE}"
printf '%s\n' "${PUSH_IMAGE_REF}" > "${NETCI_OUTPUT_DIR}/build-image-ref.txt"
