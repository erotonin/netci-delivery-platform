#!/usr/bin/env bash
# stop|start <jenkins-a|jenkins-b>: what the acceptance harness's failover gate needs.
set -euo pipefail
case "${1:-}" in
  stop)  docker stop "$2" >/dev/null ;;
  start) docker start "$2" >/dev/null ;;
  *) echo "usage: $0 stop|start <controller>" >&2; exit 2 ;;
esac
