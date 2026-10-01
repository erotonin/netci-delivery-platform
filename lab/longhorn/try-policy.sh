#!/usr/bin/env bash
# Try deploy/longhorn/tuning-policy.yaml on a throwaway copy of Longhorn's attacher before it
# touches Longhorn, counting the API server's panics (k3s 1.36.4 panics on some JSON patches).
#
#   lab/longhorn/try-policy.sh
#
# The policies are cluster-scoped: a copy under the real names replaces the real ones, and
# removing the copy removes them. That happened once and left Longhorn untuned for a chaos
# series; so the copy here is renamed as well as pointed at another namespace.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export KUBECONFIG="${ROOT}/.netci-gate/lab/kubeconfig"
NS=netci-policy-try
copy() { sed -e "s/NAMESPACE/${NS}/" -e 's/name: netci-longhorn-/name: netci-try-/' -e 's/policyName: netci-longhorn-/policyName: netci-try-/' \
  "${ROOT}/deploy/longhorn/tuning-policy.yaml"; }
copy | grep -q 'netci-longhorn-' && { echo "the copy still carries a real name" >&2; exit 1; }
panics() { for n in 1 2 3; do "${ROOT}/lab/vms.sh" ssh "$n" 'sudo journalctl -u k3s --since "-2 hours" --no-pager | grep -c "apiserver panic" || true'; done | paste -sd+ | bc; }
cleanup() { copy | kubectl delete --ignore-not-found -f - >/dev/null; kubectl delete namespace "${NS}" --ignore-not-found --wait=false >/dev/null; }
trap cleanup EXIT
kubectl create namespace "${NS}" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
copy | kubectl apply -f - >/dev/null
sleep 8
before=$(panics)
# Longhorn's attacher as Longhorn writes it: tuning removed, no replicas.
kubectl -n longhorn-system get deploy csi-attacher -o json | python3 -c '
import json, sys
d = json.load(sys.stdin)
d["metadata"] = {"name": "csi-attacher", "namespace": sys.argv[1]}
d.pop("status", None); d["spec"]["replicas"] = 0
c = d["spec"]["template"]["spec"]["containers"][0]
c["args"] = [a for a in c["args"] if not a.startswith(("--retry-interval-max=", "--leader-election-lease", "--leader-election-renew", "--leader-election-retry"))] + ["--retry-interval-max=1m"]
c["env"] = [e for e in c.get("env", []) if not e["name"].startswith("HTTP2_")]
d["spec"]["template"]["spec"].pop("topologySpreadConstraints", None)
print(json.dumps(d))' "${NS}" | kubectl apply -f - -o json >/dev/null
for i in 1 2; do kubectl -n "${NS}" annotate deploy csi-attacher --overwrite try="$i" >/dev/null; done
kubectl -n "${NS}" get deploy csi-attacher -o json | python3 -c '
import json, sys
d = json.load(sys.stdin); c = d["spec"]["template"]["spec"]["containers"][0]
print("args:", [a for a in c["args"] if a.startswith(("--retry", "--leader-election-"))])
print("env:", [e["name"] for e in c.get("env", []) if e["name"].startswith("HTTP2_")])
print("spread:", len(d["spec"]["template"]["spec"].get("topologySpreadConstraints") or []))'
after=$(panics)
echo "API server panics while trying: $((after - before))"
[[ "${after}" == "${before}" ]]
