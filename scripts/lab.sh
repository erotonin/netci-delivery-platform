#!/usr/bin/env bash
# Bring up the minimum local lab the executable gates need: PostgreSQL, an OCI registry
# and the netCI API.
#
# Two modes, because not every host can run the documented compose topology:
#
#   compose  (default) the docker-compose.yml stack. Requires a Docker daemon with a
#            usable bridge network and published ports.
#   hostnet  every container joins the host network on a high port. Use this where the
#            daemon runs with `"bridge": "none"` or `"iptables": false` (a Kolla/
#            OpenStack host, for example), which makes published ports non-functional.
#
#   scripts/lab.sh up [compose|hostnet]
#   scripts/lab.sh status
#   scripts/lab.sh down
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="${NETCI_LAB_MODE:-compose}"
PYTHON="${PYTHON:-${ROOT}/.venv/bin/python}"
[[ -x "${PYTHON}" ]] || PYTHON="python3"

POSTGRES_PORT="${NETCI_LAB_POSTGRES_PORT:-55432}"
REGISTRY_PORT="${NETCI_LAB_REGISTRY_PORT:-55000}"
API_PORT="${NETCI_LAB_API_PORT:-8100}"
POSTGRES_IMAGE="${POSTGRES_IMAGE:-postgres:16.15-alpine3.24}"
REGISTRY_IMAGE="${REGISTRY_IMAGE:-registry:3.1.1}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-netci-local-only}"
DATABASE_URL="postgresql://netci:${POSTGRES_PASSWORD}@127.0.0.1:${POSTGRES_PORT}/netci"
API_PID_FILE="${ROOT}/.netci-gate/api.pid"
API_LOG_FILE="${ROOT}/.netci-gate/api.log"

wait_for() {
  local description="$1" attempts="${2:-60}"
  shift 2
  for _ in $(seq 1 "${attempts}"); do
    if "$@" >/dev/null 2>&1; then
      echo "  ${description}: ready"
      return 0
    fi
    sleep 1
  done
  echo "  ${description}: did not become ready" >&2
  return 1
}

up_hostnet() {
  echo "starting lab (hostnet mode)"
  mkdir -p "${ROOT}/.netci-gate"

  docker rm -f -v netci-lab-postgres >/dev/null 2>&1 || true
  docker run -d --name netci-lab-postgres --network host \
    -e POSTGRES_DB=netci -e POSTGRES_USER=netci \
    -e "POSTGRES_PASSWORD=${POSTGRES_PASSWORD}" -e "PGPORT=${POSTGRES_PORT}" \
    "${POSTGRES_IMAGE}" >/dev/null
  wait_for "postgres :${POSTGRES_PORT}" 60 docker exec netci-lab-postgres pg_isready -U netci -d netci -p "${POSTGRES_PORT}"

  docker rm -f -v netci-lab-registry >/dev/null 2>&1 || true
  docker run -d --name netci-lab-registry --network host \
    -e "REGISTRY_HTTP_ADDR=0.0.0.0:${REGISTRY_PORT}" "${REGISTRY_IMAGE}" >/dev/null
  wait_for "registry :${REGISTRY_PORT}" 30 curl -sf "http://127.0.0.1:${REGISTRY_PORT}/v2/"

  echo "  applying migrations"
  DATABASE_URL="${DATABASE_URL}" "${PYTHON}" "${ROOT}/scripts/migrate.py"

  if [[ -f "${API_PID_FILE}" ]] && kill -0 "$(cat "${API_PID_FILE}")" 2>/dev/null; then
    kill "$(cat "${API_PID_FILE}")" 2>/dev/null || true
    sleep 1
  fi
  DATABASE_URL="${DATABASE_URL}" \
  NETCI_PIPELINE_API_KEY="${NETCI_PIPELINE_API_KEY:-netci-local-pipeline-key}" \
  NETCI_ALLOWED_ORIGINS="${NETCI_ALLOWED_ORIGINS:-http://localhost:5173}" \
  PYTHONPATH="${ROOT}/backend" \
    nohup "${PYTHON}" -m uvicorn app.main:app --host 127.0.0.1 --port "${API_PORT}" \
    > "${API_LOG_FILE}" 2>&1 &
  echo $! > "${API_PID_FILE}"
  wait_for "netci-api :${API_PORT}" 40 curl -sf "http://127.0.0.1:${API_PORT}/healthz"

  cat <<EOF

lab is up. Point the gates at it:

  export NETCI_API_URL=http://127.0.0.1:${API_PORT}
  export NETCI_REGISTRY=127.0.0.1:${REGISTRY_PORT}
  export NETCI_TEST_DATABASE_URL='${DATABASE_URL}'

  make security-test dora-dashboard
  make e2e-container NETCI_LAB_NETWORK_MODE=host
  make e2e-systemd
  make kind-up && REGISTRY_MODE=gateway REGISTRY_PORT=${REGISTRY_PORT} \\
      REGISTRY_PULL_HOST=localhost:${REGISTRY_PORT} bash infra/kind/local-registry.sh
  make e2e-kubernetes NETCI_REGISTRY=localhost:${REGISTRY_PORT}
EOF
}

up_compose() {
  echo "starting lab (compose mode)"
  docker compose --project-directory "${ROOT}" up -d postgres registry minio temporalite netci-api
  wait_for "netci-api" 60 curl -sf "http://127.0.0.1:${NETCI_API_HOST_PORT:-8000}/healthz"
  echo "lab is up; the gates default to http://127.0.0.1:${NETCI_API_HOST_PORT:-8000}"
}

status() {
  docker ps --filter name=netci-lab- --format 'table {{.Names}}\t{{.Status}}' || true
  if [[ -f "${API_PID_FILE}" ]] && kill -0 "$(cat "${API_PID_FILE}")" 2>/dev/null; then
    echo "netci-api: running (pid $(cat "${API_PID_FILE}"), log ${API_LOG_FILE})"
  else
    echo "netci-api: not running under ${API_PID_FILE}"
  fi
}

down() {
  if [[ -f "${API_PID_FILE}" ]]; then
    kill "$(cat "${API_PID_FILE}")" 2>/dev/null || true
    rm -f "${API_PID_FILE}"
  fi
  docker rm -f -v netci-lab-postgres netci-lab-registry >/dev/null 2>&1 || true
  docker compose --project-directory "${ROOT}" down 2>/dev/null || true
  echo "lab stopped (the kind cluster is left alone; use 'make kind-down')"
}

case "${1:-up}" in
  up)
    case "${2:-${MODE}}" in
      hostnet) up_hostnet ;;
      compose) up_compose ;;
      *) echo "unknown mode: ${2}; use compose or hostnet" >&2; exit 2 ;;
    esac
    ;;
  status) status ;;
  down) down ;;
  *) echo "usage: scripts/lab.sh {up [compose|hostnet]|status|down}" >&2; exit 2 ;;
esac
