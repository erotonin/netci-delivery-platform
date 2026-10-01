#!/usr/bin/env bash
# Tune Longhorn's CSI attacher for cell takeovers (ADR-060). Idempotent.
#
# What the lab showed, with every attacher log followed through a power-off:
#   - two of the three attacher replicas were on the machine that lost power (Longhorn spreads
#     them only by a weight-1 preference, and evictions pile them up);
#   - the leader, on a healthy machine, lost its leadership a few seconds into the outage: its
#     one HTTP/2 connection was to the dead machine's API server, and client-go notices a dead
#     HTTP/2 connection only after 30 s + 15 s. It exited, and no attacher was left to attach
#     the cell's volume elsewhere until a pod restarted.
# So: one attacher per node, and dead connections noticed in ~4 s (client-go reads these two
# environment variables). Longhorn's driver deployer may recreate the Deployment on an upgrade;
# run this again after one.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export KUBECONFIG="${ROOT}/.netci-gate/lab/kubeconfig"
patch='{"spec":{"template":{"spec":{
  "topologySpreadConstraints":[{"maxSkew":1,"topologyKey":"kubernetes.io/hostname","whenUnsatisfiable":"DoNotSchedule",
                               "labelSelector":{"matchLabels":{"app":"csi-attacher"}}}],
  "containers":[{"name":"csi-attacher","env":[{"name":"HTTP2_READ_IDLE_TIMEOUT_SECONDS","value":"2"},
                                              {"name":"HTTP2_PING_TIMEOUT_SECONDS","value":"2"}]}]}}}}'
kubectl -n longhorn-system patch deploy csi-attacher --type strategic -p "${patch}" >/dev/null
kubectl -n longhorn-system rollout status deploy/csi-attacher --timeout=180s
kubectl -n longhorn-system get pods -l app=csi-attacher -o custom-columns=POD:.metadata.name,NODE:.spec.nodeName
