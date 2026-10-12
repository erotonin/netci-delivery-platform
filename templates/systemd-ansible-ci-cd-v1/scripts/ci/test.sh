#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

require_command go
# The result is written for the evidence stage; a version's "automation evidence" is this.
mkdir -p "${NETCI_OUTPUT_DIR:-.}"
set +e
(cd "${NETCI_APP_DIR}" && go test -cover ./... 2>&1) | tee "${NETCI_OUTPUT_DIR:-.}/test-output.txt"
status="${PIPESTATUS[0]}"
set -e
if grep -q "no test files" "${NETCI_OUTPUT_DIR:-.}/test-output.txt" && ! grep -q "^ok" "${NETCI_OUTPUT_DIR:-.}/test-output.txt"; then
  printf '{"autoTest": "skipped", "testsRun": 0, "runner": "go test"}\n' > "${NETCI_OUTPUT_DIR:-.}/test-result.json"
else
  coverage="$(sed -n 's/.*coverage: \([0-9.]*\)% of statements.*/\1/p' "${NETCI_OUTPUT_DIR:-.}/test-output.txt" | tail -1)"
  printf '{"autoTest": "%s", "runner": "go test"%s}\n' "$([ "$status" = 0 ] && echo passed || echo failed)" "${coverage:+, \"coverage\": $coverage}" > "${NETCI_OUTPUT_DIR:-.}/test-result.json"
fi
exit "$status"
