#!/usr/bin/env bash
# Run the production acceptance harness against the real local stack. The identities,
# OIDC client and lab commit come from .netci-gate/real-local.env (NETCI_ACCEPTANCE_*).
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
cd "$here/../.."
set -a; source .netci-gate/real-local.env; set +a
export NETCI_ACCEPTANCE_API_URL="${NETCI_ACCEPTANCE_API_URL:-http://127.0.0.1:8100}"
export NETCI_ACCEPTANCE_MODULE="${NETCI_ACCEPTANCE_MODULE:-hello-container}"
export NETCI_ACCEPTANCE_COMMIT="${NETCI_ACCEPTANCE_COMMIT:-$(git -C .netci-gate/git/netci.git rev-parse HEAD)}"
export NETCI_ACCEPTANCE_RETIRED_SERVER="${NETCI_ACCEPTANCE_RETIRED_SERVER:-netci-retired-01}"
export NETCI_ACCEPTANCE_WORKER_RESTART="$here/worker-ctl.sh"
export NETCI_ACCEPTANCE_CONTROLLER_CONTROL="$here/controller-ctl.sh"
export PYTHONPATH=backend PYTHONUNBUFFERED=1
exec .venv/bin/python scripts/production_acceptance_harness.py
