#!/usr/bin/env bash
# Stand up a real Backstage instance wired to netCI, for the backstage-integration gate.
#
# The point of the gate is that Backstage is a *client* of netCI (ADR-002), so the
# instance here is deliberately stock: a scaffolded app, plus exactly two changes —
# the proxy fragment this repository documents, and the checked-in Software Template
# registered as a catalog location. Nothing netCI-specific is added to Backstage itself.
#
#   scripts/backstage_lab.sh up      scaffold (once), configure, install, start
#   scripts/backstage_lab.sh status
#   scripts/backstage_lab.sh down
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="${ROOT}/.netci-gate/backstage"
APP="${WORK}/netci-backstage"
PORT="${NETCI_BACKSTAGE_PORT:-7007}"
NETCI_API="${NETCI_BACKSTAGE_TARGET:-http://127.0.0.1:8100}"
PID_FILE="${WORK}/backstage.pid"
LOG_FILE="${WORK}/backstage.log"

log() { printf '  %s\n' "$*"; }

require_tools() {
  for tool in node yarn npx; do
    command -v "${tool}" >/dev/null 2>&1 || {
      echo "${tool} is required; Backstage needs Node 20+ and Yarn" >&2
      exit 1
    }
  done
}

scaffold() {
  if [[ -d "${APP}" ]]; then
    log "reusing the scaffolded app at ${APP}"
    return 0
  fi
  mkdir -p "${WORK}"
  log "scaffolding a Backstage app (this takes a few minutes)"
  ( cd "${WORK}" && echo "netci-backstage" | npx --yes @backstage/create-app@latest --path ./netci-backstage )
}

configure() {
  # Both edits are idempotent so `up` can be re-run.
  python3 - "${APP}/app-config.yaml" "${NETCI_API}" "${ROOT}" <<'PY'
import sys
from pathlib import Path

config_path, netci_api, repo_root = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
text = config_path.read_text(encoding="utf-8")

# Backstage 1.2x requires a token on its API routes. The gate calls the catalog and
# scaffolder APIs from localhost, so the documented local-development escape hatch is
# the right one here; a shared instance would use a real auth provider.
if "dangerouslyDisableDefaultAuthPolicy" not in text:
    text = text.replace(
        "backend:\n",
        "backend:\n  auth:\n    dangerouslyDisableDefaultAuthPolicy: true\n",
        1,
    )

# The proxy fragment this repository documents, pointed at the running netCI.
if "/netci:" not in text:
    fragment = f"""
proxy:
  reviveConsumedRequestBodies: true
  endpoints:
    /netci:
      target: {netci_api}
      changeOrigin: true
      allowedMethods: [GET, POST]
"""
    text = text.replace("\nproxy:", fragment, 1) if "\nproxy:" in text else text + fragment

# The Software Template, read straight from the repository so the scaffolder runs the
# same file the project ships rather than a copy that could drift.
if "netci-template.yaml" not in text:
    text = text.replace(
        "  locations:",
        "  locations:\n"
        "    - type: file\n"
        f"      target: {repo_root}/backstage/netci-template.yaml\n"
        "      rules:\n"
        "        - allow: [Template]",
        1,
    )

config_path.write_text(text, encoding="utf-8")
print("  app-config.yaml configured")
PY
}

install() {
  if [[ ! -d "${APP}/node_modules" ]]; then
    log "installing dependencies"
    ( cd "${APP}" && yarn install --silent )
  fi
  # `http:backstage:request` is not a built-in scaffolder action; the template needs it.
  if ! grep -q "scaffolder-backend-module-http-request" "${APP}/packages/backend/src/index.ts"; then
    log "adding the http request scaffolder action"
    ( cd "${APP}" && yarn --cwd packages/backend add @roadiehq/scaffolder-backend-module-http-request >/dev/null 2>&1 )
    python3 - "${APP}/packages/backend/src/index.ts" <<'PY2'
import sys
from pathlib import Path
path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
text = text.replace(
    "backend.start();",
    "// The netCI Software Template calls netCI over HTTP through the Backstage proxy.\n"
    "// `http:backstage:request` is not a built-in action; it comes from this module.\n"
    "backend.add(import('@roadiehq/scaffolder-backend-module-http-request/new-backend'));\n\n"
    "backend.start();",
    1,
)
path.write_text(text, encoding="utf-8")
PY2
  fi
}

up() {
  require_tools
  scaffold
  configure
  install
  down >/dev/null 2>&1 || true
  log "starting Backstage backend on :${PORT}"
  # Backend only: the gate drives the scaffolder and catalog APIs, and the frontend
  # dev server would add a webpack build for nothing.
  ( cd "${APP}" && nohup yarn workspace backend start > "${LOG_FILE}" 2>&1 & echo $! > "${PID_FILE}" )
  for _ in $(seq 1 180); do
    if curl -sf "http://127.0.0.1:${PORT}/api/catalog/entities?limit=1" >/dev/null 2>&1; then
      log "Backstage is up at http://127.0.0.1:${PORT}"
      log "run the gate: make backstage-test"
      return 0
    fi
    sleep 2
  done
  echo "Backstage did not come up; last log lines:" >&2
  tail -30 "${LOG_FILE}" >&2
  return 1
}

status() {
  if [[ -f "${PID_FILE}" ]] && kill -0 "$(cat "${PID_FILE}")" 2>/dev/null; then
    echo "backstage: running (pid $(cat "${PID_FILE}"), log ${LOG_FILE})"
  else
    echo "backstage: not running"
  fi
  curl -sf "http://127.0.0.1:${PORT}/api/catalog/entities?filter=kind=template" 2>/dev/null \
    | python3 -c 'import json,sys; print("  templates:", [e["metadata"]["name"] for e in json.load(sys.stdin)])' \
    2>/dev/null || echo "  catalog not reachable"
}

down() {
  if [[ -f "${PID_FILE}" ]]; then
    pkill -P "$(cat "${PID_FILE}")" 2>/dev/null || true
    kill "$(cat "${PID_FILE}")" 2>/dev/null || true
    rm -f "${PID_FILE}"
  fi
  pkill -f "backstage-cli package start" 2>/dev/null || true
  echo "backstage stopped"
}

case "${1:-up}" in
  up) up ;;
  status) status ;;
  down) down ;;
  *) echo "usage: scripts/backstage_lab.sh {up|status|down}" >&2; exit 2 ;;
esac
