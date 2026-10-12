#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

require_command buildah
require_file "${NETCI_APP_DIR}/Dockerfile"

build_args=()
if [[ -n "${NETCI_BASE_IMAGE}" ]]; then
  build_args+=(--build-arg "PYTHON_IMAGE=${NETCI_BASE_IMAGE}")
fi

buildah bud \
  --isolation "${BUILDAH_ISOLATION:-chroot}" \
  --tls-verify="${REGISTRY_TLS_VERIFY}" \
  --layers="${NETCI_BUILDAH_LAYERS:-false}" \
  "${build_args[@]}" \
  --tag "${PUSH_IMAGE_REF}" \
  "${NETCI_APP_DIR}"
rm -f "${OCI_ARCHIVE}"
buildah push "${PUSH_IMAGE_REF}" "oci-archive:${OCI_ARCHIVE}:${NETCI_IMAGE_NAME}"
require_file "${OCI_ARCHIVE}"
printf '%s\n' "${PUSH_IMAGE_REF}" > "${NETCI_OUTPUT_DIR}/build-image-ref.txt"
