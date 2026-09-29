#!/usr/bin/env bash
# Give every netci-corp node a fixed address on the `kind` network, and put back the one each
# control plane was created with.
#
# kind attaches nodes with a dynamic address. After a host reboot Docker hands them out again
# in whatever order the containers start, and a multi-control-plane cluster does not survive
# that: kind rewrites each node's manifests to its new address, but the etcd peer certificates
# and the member list still name the old ones, so every peer is refused ("tls: bad
# certificate"), etcd has no quorum and the API never comes back (seen 2026-09-29). A control
# plane's original address is the IP in its etcd peer certificate; that is where it goes back.
# The addresses are then static (IPAMConfig), so a later reboot keeps them.
#
#   scripts/corp/pin_node_ips.sh           # idempotent: does nothing when all are pinned
#   scripts/corp/pin_node_ips.sh --check   # exit 1 if a node is not where it must be
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RECORD="${ROOT}/.netci-gate/corp/node-ips"
NET=kind
CONTROL_PLANES=(netci-corp-control-plane netci-corp-control-plane2 netci-corp-control-plane3)
OTHERS=(netci-corp-external-load-balancer netci-corp-worker netci-corp-worker2 netci-corp-worker3)
log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }

ip_of() { docker inspect -f "{{with index .NetworkSettings.Networks \"${NET}\"}}{{.IPAddress}}{{end}}" "$1"; }
pinned_ip() { docker inspect -f "{{with index .NetworkSettings.Networks \"${NET}\"}}{{with .IPAMConfig}}{{.IPv4Address}}{{end}}{{end}}" "$1"; }
cert_ip() {  # the non-loopback IP in the node's etcd peer certificate
  docker exec "$1" openssl x509 -in /etc/kubernetes/pki/etcd/peer.crt -noout -ext subjectAltName \
    | grep -o 'IP Address:[0-9.]*' | cut -d: -f2 | grep -v '^127\.' | head -1
}

declare -A want
if [[ -s "${RECORD}" ]]; then
  while read -r name ip; do want[$name]=$ip; done < "${RECORD}"
fi
for node in "${CONTROL_PLANES[@]}"; do
  [[ -n "${want[$node]:-}" ]] || want[$node]="$(cert_ip "${node}")"
  [[ -n "${want[$node]}" ]] || { echo "cannot read the etcd certificate of ${node}" >&2; exit 1; }
done
taken=" ${want[*]} "
for node in "${OTHERS[@]}"; do
  [[ -n "${want[$node]:-}" ]] && continue
  current="$(ip_of "${node}")"
  if [[ -n "${current}" && "${taken}" != *" ${current} "* ]]; then
    want[$node]="${current}"
  else  # its address belongs to a control plane: take a free one out of the dynamic range's way
    used=" $(docker network inspect "${NET}" -f '{{range .Containers}}{{.IPv4Address}} {{end}}' | sed 's#/[0-9]*##g') ${taken} "
    for i in $(seq 20 39); do
      candidate="172.17.0.${i}"
      [[ "${used}" == *" ${candidate} "* ]] || { want[$node]="${candidate}"; break; }
    done
  fi
  taken+="${want[$node]} "
done

drift=()
for node in "${CONTROL_PLANES[@]}" "${OTHERS[@]}"; do
  [[ "$(ip_of "${node}")" == "${want[$node]}" && "$(pinned_ip "${node}")" == "${want[$node]}" ]] || drift+=("${node}")
done

if [[ "${1:-}" == "--check" ]]; then
  (( ${#drift[@]} == 0 )) || { echo "not at their pinned address: ${drift[*]}" >&2; exit 1; }
  exit 0
fi
if (( ${#drift[@]} == 0 )); then
  log "netci-corp nodes already on their pinned addresses"
  exit 0
fi

# Another container on the network holding a wanted address would make `connect` fail half-way.
for node in "${CONTROL_PLANES[@]}" "${OTHERS[@]}"; do
  holder="$(docker network inspect "${NET}" -f '{{range .Containers}}{{.Name}} {{.IPv4Address}}{{"\n"}}{{end}}' \
    | awk -v ip="${want[$node]}/" 'index($2, ip) == 1 {print $1}')"
  if [[ -n "${holder}" && "${holder}" != netci-corp-* ]]; then
    echo "${want[$node]} (wanted by ${node}) is held by ${holder}; move it first" >&2; exit 1
  fi
done

log "pinning ${#drift[@]} node(s): ${drift[*]} (the whole cluster restarts)"
all=("${OTHERS[@]}" "${CONTROL_PLANES[@]}")
docker stop -t 30 "${all[@]}" >/dev/null
for node in "${all[@]}"; do
  docker network disconnect "${NET}" "${node}" 2>/dev/null || true
done
for node in "${all[@]}"; do
  docker network connect --ip "${want[$node]}" "${NET}" "${node}"
done
# Written before the nodes start: the addresses are decided even if a start fails.
( umask 077; for node in "${all[@]}"; do echo "${node} ${want[$node]}"; done > "${RECORD}" )
docker start netci-corp-external-load-balancer >/dev/null
docker start "${CONTROL_PLANES[@]}" >/dev/null
docker start netci-corp-worker netci-corp-worker2 netci-corp-worker3 >/dev/null
for node in "${all[@]}"; do log "  ${node} ${want[$node]}"; done
