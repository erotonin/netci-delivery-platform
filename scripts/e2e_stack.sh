#!/usr/bin/env bash
# The netCI the browser suite runs against: the real API (demo data on) and a real edge agent.
#
#   DATABASE_URL=postgresql://... scripts/e2e_stack.sh start|stop
#
# The agent is backend/app/adapters/agent_daemon.py, connected as localhost
# (a server of the demo data) with a token minted through the API like any agent's. It reports
# this machine's own CPU, memory and disk and runs the commands its policy allows, so the
# suite's telemetry and remote-execution tests see real figures and real output -- never
# invented ones (the telemetry endpoint answers 404 until an agent has reported).
# The signing key is generated for this run and never written anywhere; the token is never
# printed. PIDs and logs go to ${E2E_STATE:-.netci-gate/e2e}.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE="${E2E_STATE:-${ROOT}/.netci-gate/e2e}"
PY="${PYTHON:-$(test -x "${ROOT}/.venv/bin/python" && echo "${ROOT}/.venv/bin/python" || echo python3)}"
API_PORT="${NETCI_E2E_API_PORT:-8100}"
HOSTNAME_="${NETCI_E2E_AGENT_HOSTNAME:-localhost}"
mkdir -p "${STATE}"
# One directory for the whole script: a background "cd X && cmd &" makes $! the subshell, not cmd,
# and stop then killed the subshell and left the API and the agent running.
cd "${ROOT}"

stop() {
  for f in agent api; do
    if [[ -s "${STATE}/${f}.pid" ]]; then kill "$(cat "${STATE}/${f}.pid")" 2>/dev/null || true; rm -f "${STATE}/${f}.pid"; fi
  done
}

start() {
  : "${DATABASE_URL:?set DATABASE_URL to a database the suite may write to (never a live one)}"
  stop
  "${PY}" scripts/migrate.py >/dev/null
  key="e2e:$(openssl rand -hex 32)"
  PYTHONPATH=backend NETCI_ALLOWED_ORIGINS="http://127.0.0.1:${NETCI_PORTAL_PORT:-4173}" NETCI_DEMO_DATA=true \
    NETCI_WORKLOAD_TOKEN_KEYS="${key}" nohup "${PY}" -m uvicorn app.main:app --host 127.0.0.1 --port "${API_PORT}" \
    >"${STATE}/api.log" 2>&1 &
  echo $! >"${STATE}/api.pid"
  for _ in $(seq 1 60); do curl -sf "http://127.0.0.1:${API_PORT}/healthz" >/dev/null && break; sleep 1; done
  curl -sf "http://127.0.0.1:${API_PORT}/healthz" >/dev/null || { tail -20 "${STATE}/api.log"; exit 1; }
  token="$(curl -sf -X POST "http://127.0.0.1:${API_PORT}/api/v1/agents/token" -H 'Content-Type: application/json' \
      -d "{\"hostname\":\"${HOSTNAME_}\",\"ttlSeconds\":7200}" | "${PY}" -c 'import json,sys; print(json.load(sys.stdin)["token"])')"
  NETCI_AGENT_TOKEN="${token}" PYTHONPATH=backend nohup "${PY}" backend/app/adapters/agent_daemon.py \
    --server "ws://127.0.0.1:${API_PORT}" --hostname "${HOSTNAME_}" --heartbeat 2 >"${STATE}/agent.log" 2>&1 &
  echo $! >"${STATE}/agent.pid"
  unset token
  for _ in $(seq 1 30); do
    curl -sf "http://127.0.0.1:${API_PORT}/api/v1/servers/${HOSTNAME_}/telemetry" >/dev/null && { echo "netCI and an agent are up on :${API_PORT}"; return 0; }
    sleep 1
  done
  echo "the agent did not report telemetry:"; tail -20 "${STATE}/agent.log"; exit 1
}

case "${1:-}" in
  start) start ;;
  stop) stop ;;
  *) echo "usage: $0 start|stop" >&2; exit 2 ;;
esac
