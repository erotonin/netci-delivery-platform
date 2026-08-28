#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

require_command go
require_command sha256sum

# Build inside the application directory: the Go module lives there, and the
# inner build script writes dist/ relative to its working directory.
(cd "${NETCI_APP_DIR}" && VERSION="${VERSION}" bash build.sh)
require_file "${ARTIFACT_PATH}"

# For a binary artifact the immutable identity is the content hash, so it is known
# as soon as the artifact exists. Publish later verifies it rather than inventing one.
printf 'sha256:%s\n' "$(sha256sum "${ARTIFACT_PATH}" | awk '{print $1}')" > "${NETCI_OUTPUT_DIR}/artifact-digest.txt"
require_file "${NETCI_OUTPUT_DIR}/artifact-digest.txt"
