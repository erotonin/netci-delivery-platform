#!/usr/bin/env bash
# Tune Longhorn's CSI attacher and managers for cell takeovers (ADR-060). Idempotent.
#
# What the lab showed, with every attacher log followed through a power-off:
#   - two of the three attacher replicas were on the machine that lost power (Longhorn spreads
#     them only by a weight-1 preference, and evictions pile them up);
#   - the leader, on a healthy machine, lost its leadership a few seconds into the outage: its
#     one HTTP/2 connection was to the dead machine's API server, and client-go notices a dead
#     HTTP/2 connection only after 30 s + 15 s. It exited, and no attacher was left to attach
#     the cell's volume elsewhere until a pod restarted.
#   - the next trace (lab/spike/trace_takeover.sh) showed the same blindness in longhorn-manager:
#     the cell's volume had a replica on a healthy machine, only that machine's manager may stop
#     it, and its watches sat on a connection to the dead API server for 44 s. Nothing could
#     detach the volume until it noticed. The attacher's back-off (up to 1 min) then added ~20 s.
# (The spread counts only pods of the same revision: counted with the old ones a rollout put two
# new attachers on one machine.)
# So: one attacher per node, retries at most 5 s apart, and dead connections noticed in ~4 s by
# the attacher and every manager (client-go reads these two environment variables). Longhorn's
# driver deployer may recreate the attacher Deployment on an upgrade, and re-applying longhorn.yaml
# resets the managers; run this again after either.
#
# This blindness exists because every lab machine is also a control-plane node, so a power loss
# takes an API server with it. Cells on machines that serve no API server do not meet it.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export KUBECONFIG="${ROOT}/.netci-gate/lab/kubeconfig"
patch='{"spec":{"template":{"spec":{
  "topologySpreadConstraints":[{"maxSkew":1,"topologyKey":"kubernetes.io/hostname","whenUnsatisfiable":"DoNotSchedule",
                               "labelSelector":{"matchLabels":{"app":"csi-attacher"}},
                               "matchLabelKeys":["pod-template-hash"]}],
  "containers":[{"name":"csi-attacher","env":[{"name":"HTTP2_READ_IDLE_TIMEOUT_SECONDS","value":"2"},
                                              {"name":"HTTP2_PING_TIMEOUT_SECONDS","value":"2"}]}]}}}}'
kubectl -n longhorn-system patch deploy csi-attacher --type strategic -p "${patch}" >/dev/null
args=$(kubectl -n longhorn-system get deploy csi-attacher -o json | python3 -c '
import json, sys
c = json.load(sys.stdin)["spec"]["template"]["spec"]["containers"][0]
args = [a for a in c["args"] if not a.startswith("--retry-interval-max=")] + ["--retry-interval-max=5s"]
print(json.dumps([{"op": "replace", "path": "/spec/template/spec/containers/0/args", "value": args}]))')
kubectl -n longhorn-system patch deploy csi-attacher --type json -p "${args}" >/dev/null
kubectl -n longhorn-system rollout status deploy/csi-attacher --timeout=180s
manager='{"spec":{"template":{"spec":{"containers":[{"name":"longhorn-manager","env":[
  {"name":"HTTP2_READ_IDLE_TIMEOUT_SECONDS","value":"2"},{"name":"HTTP2_PING_TIMEOUT_SECONDS","value":"2"}]}]}}}}'
kubectl -n longhorn-system patch ds longhorn-manager --type strategic -p "${manager}" >/dev/null
kubectl -n longhorn-system rollout status ds/longhorn-manager --timeout=600s
kubectl -n longhorn-system get pods -l app=csi-attacher -o custom-columns=POD:.metadata.name,NODE:.spec.nodeName
