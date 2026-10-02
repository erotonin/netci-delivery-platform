#!/usr/bin/env bash
# Install netCI on the lab with its Helm charts, as an organisation would, adopting what the lab
# scripts installed so that nothing stops on the way.
#
#   NETCI_IMAGE=172.17.0.1:8930/netci/netci@sha256:<digest> [FENCE=redfish] [MONITORING=on] lab/helm-install.sh
#
# MONITORING=on adds the PodMonitors, alerts and dashboard (needs lab/monitoring.sh install first).
#
# Values come from what the lab scripts generated (.netci-gate/lab/{queue,fabric}/config.json:
# token hashes only) and go to .netci-gate/lab/helm/, never into the repository. Objects that
# already exist under a chart's names are labelled and annotated as Helm's (adoption), then
# "helm upgrade --install" takes them over; a cell's StatefulSet keeps its volume (same claim
# template, same names). PostgreSQL (lab/queue.sh) stays: the charts take a database, as they
# would an organisation's own.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE="${ROOT}/.netci-gate/lab"
H="${STATE}/helm"
export KUBECONFIG="${STATE}/kubeconfig"
: "${NETCI_IMAGE:?set NETCI_IMAGE to the netCI image, pinned by digest}"
REPO="${NETCI_IMAGE%@*}" DIGEST="${NETCI_IMAGE#*@}"
CONTROLLER="$(python3 -c 'import yaml,sys; d=yaml.safe_load(open(sys.argv[1])); print(d["image"]["controller"])' "${ROOT}/deploy/helm/netci-cell/ci/lab-values.yaml")"
mkdir -m 700 -p "${H}"

python3 - "${STATE}" "${H}/netci.yaml" <<'PY'
import json, sys, yaml
state, out = sys.argv[1], sys.argv[2]
queue = json.load(open(f"{state}/queue/config.json"))
fabric = json.load(open(f"{state}/fabric/config.json"))
for k in ("namespace", "serviceAccount", "audience", "bootstrapImage", "fabricUrl", "priorityClass"):
    fabric.pop(k, None)  # the chart decides these
cells = sorted(queue["cells"])
yaml.safe_dump({
    "imagePullSecrets": [{"name": "harbor-pull"}],
    "supervisor": {"machines": "netci-lab-1=netci-lab-1,netci-lab-2=netci-lab-2,netci-lab-3=netci-lab-3",
                   "fence": {"addr": "192.168.122.1:22", "user": "deployer", "secretName": "netci-fence"},
                   "autoPowerOn": True},
    "queue": {"database": {"secretName": "netci-queue-db"}, "config": queue,
              "cellCredentials": {"secretName": "netci-queue-cells",
                                  "items": [i for c in cells for i in ({"key": f"{c}-user", "path": f"{c}/user"},
                                                                       {"key": f"{c}-token", "path": f"{c}/token"})]},
              "service": {"type": "NodePort", "nodePort": 30090}},
    "fabric": {"database": {"secretName": "netci-queue-db"}, "config": fabric,
               # k3s pods and services, the lab's machines: sandboxes may reach none of them.
               "networkPolicy": {"clusterCIDRs": ["10.42.0.0/16", "10.43.0.0/16", "192.168.122.0/24"]}},
}, open(out, "w"))
PY

# adopt RELEASE NAMESPACE: every object the release renders that already exists becomes Helm's.
adopt() {
  local release=$1 ns=$2; shift 2
  helm template "${release}" "$@" -n "${ns}" | python3 -c '
import sys, yaml
for d in yaml.safe_load_all(sys.stdin):
    if d:
        print(d["kind"], d["metadata"].get("namespace") or "-", d["metadata"]["name"])' |
  while read -r kind objns name; do
    nsflag=(); [[ "${objns}" != "-" ]] && nsflag=(-n "${objns}")
    kubectl get "${kind}" "${name}" "${nsflag[@]}" >/dev/null 2>&1 || continue
    kubectl label "${kind}" "${name}" "${nsflag[@]}" app.kubernetes.io/managed-by=Helm --overwrite >/dev/null
    kubectl annotate "${kind}" "${name}" "${nsflag[@]}" meta.helm.sh/release-name="${release}" \
      meta.helm.sh/release-namespace="${ns}" --overwrite >/dev/null
  done
}

platform=(deploy/helm/netci -f "${H}/netci.yaml" --set image.repository="${REPO}" --set image.digest="${DIGEST}")
# FENCE=redfish: fence through the Redfish emulator (lab/redfish.sh) instead of the SSH agent.
if [[ "${FENCE:-ssh}" == redfish ]]; then
  "${ROOT}/lab/redfish.sh" secret > "${H}/fence-redfish.yaml"
  platform+=(-f "${H}/fence-redfish.yaml")
fi
if [[ "${MONITORING:-off}" == on ]]; then
  platform+=(--set monitoring.enabled=true --set monitoring.dashboard.enabled=true)
fi
(cd "${ROOT}" && adopt netci netci-system "${platform[@]}")
(cd "${ROOT}" && helm upgrade --install netci "${platform[@]}" -n netci-system --wait --timeout 10m)

# The cells: storage class and UI port as lab/spike/deploy.sh gave them.
for cell in cell-a cell-b; do
  case "${cell}" in cell-a) class=longhorn-sync port=30080 ;; cell-b) class=longhorn-commit1 port=30081 ;; esac
  args=(deploy/helm/netci-cell -f "${ROOT}/deploy/helm/netci-cell/ci/lab-values.yaml"
        --set image.controller="${CONTROLLER}" --set netci.repository="${REPO}" --set netci.digest="${DIGEST}"
        --set storage.className="${class}" --set ui.nodePort="${port}" --set kubernetesPlugin.enabled=true
        --set monitoring.enabled="$([[ "${MONITORING:-off}" == on ]] && echo true || echo false)")
  if [[ "${MONITORING:-off}" == on ]]; then
    # Build logs to the lab's Loki (lab/monitoring.sh), readable while the cell is taken over.
    args+=(--set logShipping.enabled=true --set logShipping.lokiUrl=http://192.168.122.1:3100
           --set logShipping.image=cr.fluentbit.io/fluent/fluent-bit@sha256:c5542543523c9678398dd78d927c05e8425ec15b038f226b67b3b019b1a70845)
  fi
  # The lab's NodePort Service holds the port the chart's jenkins-ui takes.
  kubectl -n "${cell}" delete service jenkins-np --ignore-not-found >/dev/null
  (cd "${ROOT}" && adopt "${cell}" "${cell}" "${args[@]}")
  (cd "${ROOT}" && helm upgrade --install "${cell}" "${args[@]}" -n "${cell}" --wait --timeout 15m)
done
helm list -A
