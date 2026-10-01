#!/usr/bin/env bash
# The control plane's own failover timings on the lab, for one experiment (ADR-060).
#
#   lab/k3s-timings.sh upstream   etcd and leader elections at upstream defaults
#   lab/k3s-timings.sh k3s        back to k3s's own (no override)
#
# Every lab machine is a control-plane node, so a power loss also takes an etcd member, often
# etcd's leader, and often kube-controller-manager's and kube-scheduler's leaders. k3s waits
# 5 s before electing a new etcd leader (upstream etcd: 1 s), and the two controllers' leases
# are 15 s. The chaos series measured those waits inside every takeover. A control plane on
# machines without cells does not have them at all. These timings let the lab show roughly what
# a takeover costs without them. They are not a recommendation for small or slow clusters,
# where k3s's longer timeouts keep the control plane from electing leaders on a slow disk.
#
# Servers are restarted one at a time, each Ready again before the next; running pods keep
# running through a k3s restart.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export KUBECONFIG="${ROOT}/.netci-gate/lab/kubeconfig"
case "${1:-}" in
  upstream) conf='etcd-arg: ["election-timeout=1000", "heartbeat-interval=100"]
kube-controller-manager-arg: ["leader-elect-lease-duration=6s", "leader-elect-renew-deadline=4s", "leader-elect-retry-period=1s"]
kube-scheduler-arg: ["leader-elect-lease-duration=6s", "leader-elect-renew-deadline=4s", "leader-elect-retry-period=1s"]' ;;
  k3s) conf='' ;;
  *) sed -n '2,6p' "$0" >&2; exit 2 ;;
esac
for i in 1 2 3; do
  n="netci-lab-$i"
  if [[ -n "${conf}" ]]; then
    printf '%s\n' "${conf}" | "${ROOT}/lab/vms.sh" ssh "$i" 'sudo mkdir -p /etc/rancher/k3s/config.yaml.d && sudo tee /etc/rancher/k3s/config.yaml.d/50-netci-timings.yaml >/dev/null'
  else
    "${ROOT}/lab/vms.sh" ssh "$i" 'sudo rm -f /etc/rancher/k3s/config.yaml.d/50-netci-timings.yaml'
  fi
  "${ROOT}/lab/vms.sh" ssh "$i" 'sudo systemctl restart k3s'
  until kubectl get node "$n" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null | grep -q True; do sleep 3; done
  until "${ROOT}/lab/vms.sh" ssh "$i" 'sudo grep -q "election-timeout: '"$([[ -n "${conf}" ]] && echo 1000 || echo 5000)"'" /var/lib/rancher/k3s/server/db/etcd/config'; do sleep 3; done
  echo "${n}: restarted with the ${1} timings"
  sleep 20 # etcd and the controllers settle before the next member goes
done
