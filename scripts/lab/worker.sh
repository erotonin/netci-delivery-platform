#!/usr/bin/env bash
# Run the Temporal worker against the real local stack (profile: .netci-gate/real-local.env).
set -euo pipefail
cd "$(dirname "$0")/../.."
set -a; source .netci-gate/real-local.env; set +a
export NETCI_API_URL="${NETCI_API_URL:-http://127.0.0.1:8100}" NETCI_PROJECT_ROOT="$PWD" PYTHONPATH=backend
export NETCI_ANSIBLE_INVENTORY="${NETCI_ANSIBLE_INVENTORY:-$PWD/deploy/ansible/inventories/localhost.ini}"
exec .venv/bin/python -m app.workflows.worker
