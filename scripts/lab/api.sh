#!/usr/bin/env bash
# Run the netCI API against the real local stack (profile: .netci-gate/real-local.env).
# The profile is local-only and carries the lab's secrets; it is never committed.
set -euo pipefail
cd "$(dirname "$0")/../.."
set -a; source .netci-gate/real-local.env; set +a
export PYTHONPATH=backend NETCI_API_URL="${NETCI_API_URL:-http://127.0.0.1:8100}"
exec .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port "${NETCI_API_PORT:-8100}" --log-level info
