#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

require_command go
VERSION="${VERSION}" bash "${NETCI_APP_DIR}/build.sh"
require_file "${ARTIFACT_PATH}"
