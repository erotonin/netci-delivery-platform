#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

require_command python3
python3 -m compileall -q "${NETCI_APP_DIR}"
if compgen -G "${NETCI_APP_DIR}/test*.py" >/dev/null; then
  python3 -m unittest discover -s "${NETCI_APP_DIR}" -p 'test*.py'
fi
