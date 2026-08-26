#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

require_command go
(cd "${NETCI_APP_DIR}" && go test ./...)
