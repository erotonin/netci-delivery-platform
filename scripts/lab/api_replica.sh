#!/usr/bin/env bash
# A second API replica against the same database: api_replica.sh <name> <port>
# What ADR-032 needs to be exercised: agents connected to one replica, commands from the
# other, one reconciler pass at a time.
set -euo pipefail
cd "$(dirname "$0")/../.."
name="${1:?replica name}" port="${2:?port}"
NETCI_REPLICA_ID="$name" NETCI_API_PORT="$port" NETCI_API_URL="http://127.0.0.1:$port" exec scripts/lab/api.sh
