#!/usr/bin/env bash
# One unattended power-off with every log on the takeover's path followed throughout: Longhorn's
# managers and CSI attachers, and the events of the cell and of longhorn-system. Writes them
# under the directory given (default: a new one in /tmp) for reading the timeline afterwards.
#
#   lab/spike/trace_takeover.sh [dir]
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export KUBECONFIG="${ROOT}/.netci-gate/lab/kubeconfig"
OUT="${1:-$(mktemp -d)}"; mkdir -p "${OUT}"
declare -A IPS=([netci-lab-1]=192.168.122.211 [netci-lab-2]=192.168.122.212 [netci-lab-3]=192.168.122.213)
# Talk to an API server that is not on the machine about to lose power: with the default one,
# every followed log was cut the moment that machine went (the first trace recorded nothing).
cellnode=$(kubectl -n cell-b get pod jenkins-0 -o jsonpath='{.spec.nodeName}')
for n in netci-lab-1 netci-lab-2 netci-lab-3; do [[ "$n" != "$cellnode" ]] && { server="https://${IPS[$n]}:6443"; break; }; done
k() { kubectl --server="${server}" "$@"; }
pids=()
follow() {  # namespace selector; a follower that is cut starts again, overlapping a little
  for p in $(k -n "$1" get pods -l "$2" -o name); do
    n=${p#pod/}; node=$(k -n "$1" get "$p" -o jsonpath='{.spec.nodeName}')
    c=$(k -n "$1" get "$p" -o jsonpath='{.spec.containers[0].name}')
    ( while true; do k -n "$1" logs -f --timestamps "$p" -c "$c" --since=15s 2>/dev/null; sleep 1; done ) >> "${OUT}/${n}@${node}.log" & pids+=($!)
  done
}
follow longhorn-system app=longhorn-manager
follow longhorn-system app=csi-attacher
for ns in cell-b longhorn-system; do
  k -n "$ns" get events -w -o custom-columns=T:.lastTimestamp,OBJ:.involvedObject.name,R:.reason,M:.message --no-headers > "${OUT}/events-${ns}.log" 2>&1 & pids+=($!)
done
trap 'kill "${pids[@]}" 2>/dev/null; pkill -P $$ 2>/dev/null || true' EXIT
sleep 2
NETCI_CELL=cell-b NETCI_CELL_PORT=30081 python3 "${ROOT}/lab/spike/probe.py" resume --failure node-poweroff-supervised \
  --seconds 120 --crash-at-tick 15 > "${OUT}/probe.out" 2>&1 || true
echo "${OUT}"
