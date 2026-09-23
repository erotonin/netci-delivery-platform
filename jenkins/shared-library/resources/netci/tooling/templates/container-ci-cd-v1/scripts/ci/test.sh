#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

require_command python3
python3 -m compileall -q "${NETCI_APP_DIR}"
# The result is written for the evidence stage: "passed" only when tests ran and passed,
# "skipped" when the application has none. A version's "automation evidence" is this.
mkdir -p "${NETCI_OUTPUT_DIR:-.}"
if compgen -G "${NETCI_APP_DIR}/test*.py" >/dev/null; then
  python3 -m unittest discover -s "${NETCI_APP_DIR}" -p 'test*.py' 2>&1 | tee "${NETCI_OUTPUT_DIR:-.}/test-output.txt"
  status="${PIPESTATUS[0]}"
  ran="$(sed -n 's/^Ran \([0-9]*\) test.*/\1/p' "${NETCI_OUTPUT_DIR:-.}/test-output.txt" | tail -1)"
  printf '{"autoTest": "%s", "testsRun": %s, "runner": "unittest"}\n' "$([ "$status" = 0 ] && echo passed || echo failed)" "${ran:-0}" > "${NETCI_OUTPUT_DIR:-.}/test-result.json"
  exit "$status"
else
  printf '{"autoTest": "skipped", "testsRun": 0, "runner": "unittest"}\n' > "${NETCI_OUTPUT_DIR:-.}/test-result.json"
fi
