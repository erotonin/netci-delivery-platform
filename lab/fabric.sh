#!/usr/bin/env bash
# Install the agent fabric on the lab cluster and give cell-b a token for it.
#
#   NETCI_IMAGE=172.17.0.1:8930/netci/netci@sha256:<digest> lab/fabric.sh
#
# Needs lab/queue.sh first (the database). Writes .netci-gate/lab/fabric/cell-b-token, which
# lab/spike/deploy.sh mounts into cell-b as /run/secrets/cell/fabric-token.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE="${ROOT}/.netci-gate/lab"
F="${STATE}/fabric"
export KUBECONFIG="${STATE}/kubeconfig"
: "${NETCI_IMAGE:?set NETCI_IMAGE to the netCI image, pinned by digest}"
AGENT_IMAGE="${AGENT_IMAGE:-172.17.0.1:8930/mirror/jenkins/inbound-agent:3386.v353e57a_1b_ea_0-1-jdk21}"
mkdir -m 700 -p "${F}"
( umask 077; [[ -s "${F}/cell-b-token" ]] || openssl rand -hex 32 > "${F}/cell-b-token" )
hash="$(tr -d '\n' < "${F}/cell-b-token" | sha256sum | cut -d' ' -f1)"
cat > "${F}/config.json" <<JSON
{"namespace": "netci-agents", "serviceAccount": "netci-sandbox", "audience": "netci-fabric",
 "bootstrapImage": "${NETCI_IMAGE}", "fabricUrl": "http://netci-fabric.netci-system.svc.cluster.local:8080",
 "pullSecrets": ["harbor-pull"], "priorityClass": "netci-sandbox",
 "pools": [{"name": "standard", "labels": ["netci-standard"], "image": "${AGENT_IMAGE}",
            "warm": 2, "max": 6, "cpu": "1", "memory": "1Gi", "disk": "4Gi", "userNamespace": true}],
 "cells": {"cell-b": "${hash}"}}
JSON
kubectl apply -f "${ROOT}/lab/priorities.yaml" >/dev/null
sed -e "s|NETCI_IMAGE|${NETCI_IMAGE}|" "${ROOT}/lab/fabric/fabric.yaml" | kubectl apply -f - >/dev/null
kubectl -n netci-agents create secret generic harbor-pull --type kubernetes.io/dockerconfigjson \
  --from-file=.dockerconfigjson="${STATE}/harbor-pull.json" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl -n netci-system create configmap netci-fabric --from-file=config.json="${F}/config.json" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl -n netci-system rollout restart deployment/netci-fabric >/dev/null
kubectl -n netci-system rollout status deployment/netci-fabric --timeout=300s
