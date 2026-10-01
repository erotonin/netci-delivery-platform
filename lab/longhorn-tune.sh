#!/usr/bin/env bash
# Tune Longhorn's CSI attacher and managers for cell takeovers (ADR-066). Idempotent.
#
# What the lab showed, with every attacher and manager log followed through power-offs
# (lab/spike/trace_takeover.sh):
#   - two of the three attacher replicas were on the machine that lost power, and the leader, on
#     a healthy machine, lost its leadership: its one HTTP/2 connection was to the dead machine's
#     API server, and client-go notices a dead HTTP/2 connection only after 30 s + 15 s;
#   - the same blindness in the managers: the cell's volume had a replica on a healthy machine,
#     only that machine's manager may stop it, and its watches sat on a dead connection for 44 s;
#   - the attacher's back-off (up to 1 min) then added ~20 s.
# So: one attacher per node, retries at most 5 s apart, and dead connections noticed in ~4 s by
# the attacher and every manager (client-go reads two environment variables).
#
# Applied as admission policies (deploy/longhorn/tuning-policy.yaml), not as a patch: Longhorn's
# driver deployer rewrites the attacher's Deployment every time it starts -- after any takeover
# of its machine -- and a patch was undone by the next one. The policies tune whatever Longhorn
# writes. An update is made here so that what exists now is tuned too.
#
# This blindness exists because every lab machine is also a control-plane node, so a power loss
# takes an API server with it. Cells on machines that serve no API server do not meet it.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export KUBECONFIG="${ROOT}/.netci-gate/lab/kubeconfig"
sed 's/NAMESPACE/longhorn-system/' "${ROOT}/deploy/longhorn/tuning-policy.yaml" | kubectl apply -f - >/dev/null
sleep 5 # the API servers load the policies
kubectl -n longhorn-system rollout restart deploy/csi-attacher >/dev/null
kubectl -n longhorn-system rollout status deploy/csi-attacher --timeout=300s
if ! kubectl -n longhorn-system get ds longhorn-manager -o jsonpath='{.spec.template.spec.containers[0].env[*].name}' | grep -q HTTP2_READ_IDLE; then
  kubectl -n longhorn-system rollout restart ds/longhorn-manager >/dev/null
  kubectl -n longhorn-system rollout status ds/longhorn-manager --timeout=600s
fi
# Longhorn deletes a pod whose volume it re-attached, judging by start times; a replacement that
# a fast takeover had started first was deleted too (2 of 6 runs, 35-55 s each). Cells' pods are
# restarted by netCI instead, from the cell agent's own write probe; StatefulSets are left out of
# Longhorn's deletions.
kubectl -n longhorn-system patch settings.longhorn.io blacklist-for-auto-delete-pod-when-volume-detached-unexpectedly \
  --type merge -p '{"value":"apps/StatefulSet"}' >/dev/null
kubectl -n longhorn-system get deploy csi-attacher -o jsonpath='{.spec.template.spec.containers[0].args}{"\n"}' | grep -q -- '--retry-interval-max=2s' \
  || { echo "the attacher is not tuned: are the admission policies active?" >&2; exit 1; }
kubectl -n longhorn-system get pods -l app=csi-attacher -o custom-columns=POD:.metadata.name,NODE:.spec.nodeName
