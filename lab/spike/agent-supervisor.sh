#!/usr/bin/env bash
# Keeps an inbound Jenkins agent attached to whichever controller is serving its cell now.
#
# Why (spike, 2026-10-01): when a controller's machine loses power no FIN or RST ever reaches
# the agent, so its WebSocket stays "open" to a host that no longer exists. Jenkins' channel
# pinger notices after 5 min + 4 min, and a resumed build waits only ~5 min for its agent: the
# build failed with "Timeout waiting for agent to come back" although the agent was alive.
#
# Every Jenkins start has a new X-Jenkins-Session. This watches it through the cell's stable
# address and restarts only the agent JVM when it changes, so the agent reconnects to the new
# controller within seconds. The build's own processes are started by durable-task outside the
# agent JVM and keep running through the restart; the resumed step re-attaches to their output.
#
# A session that disappears and comes back unchanged was a network blip: the agent is left
# alone. Environment is the inbound agent's own (JENKINS_URL, JENKINS_SECRET, ...).
set -uo pipefail
url="${JENKINS_URL%/}"
interval="${NETCI_SESSION_POLL_SECONDS:-2}"
agent_pid=""

session() {
  curl -s -o /dev/null -D - --max-time 3 "${url}/login" 2>/dev/null \
    | tr -d '\r' | awk -F': ' 'tolower($1)=="x-jenkins-session" {print $2}'
}
log() { echo "netci-agent-supervisor: $*" >&2; }
stop_agent() {
  [[ -n "${agent_pid}" ]] || return 0
  kill -TERM "${agent_pid}" 2>/dev/null
  for _ in 1 2 3 4 5; do kill -0 "${agent_pid}" 2>/dev/null || break; sleep 1; done
  kill -KILL "${agent_pid}" 2>/dev/null
  wait "${agent_pid}" 2>/dev/null
  agent_pid=""
}
trap 'stop_agent; exit 0' TERM INT

while true; do
  current=""
  until current=$(session) && [[ -n "${current}" ]]; do sleep 1; done
  log "controller session ${current:0:8}: starting agent"
  /usr/local/bin/jenkins-agent "$@" &
  agent_pid=$!
  while kill -0 "${agent_pid}" 2>/dev/null; do
    sleep "${interval}"
    seen=$(session)
    if [[ -n "${seen}" && "${seen}" != "${current}" ]]; then
      log "controller replaced (session ${current:0:8} -> ${seen:0:8}): restarting agent to reconnect"
      stop_agent
      break
    fi
  done
  if [[ -n "${agent_pid}" ]]; then  # the agent exited by itself
    wait "${agent_pid}" 2>/dev/null; rc=$?; agent_pid=""
    log "agent exited (${rc}); retrying in 5 s"
    sleep 5
  fi
done
