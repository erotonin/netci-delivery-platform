#!/usr/bin/env bash
# stop|start the Temporal worker: what the acceptance harness's restart/resume gate needs.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
case "${1:-}" in
  stop)  pkill -f "python -m app.workflows.worker" || true; sleep 1 ;;
  start) nohup "$here/worker.sh" >> "${NETCI_LAB_LOG_DIR:-/tmp}/netci-worker.log" 2>&1 & disown; sleep 2 ;;
  *) echo "usage: $0 stop|start" >&2; exit 2 ;;
esac
