#!/usr/bin/env bash
# Kata Containers on one lab machine, so that the fabric's `untrusted` sandboxes run each build in
# its own virtual machine with its own kernel (ADR-061), on the lab's nested virtualisation.
#
#   lab/kata.sh install [node]   default netci-lab-3; installs one shim, QEMU on the Rust runtime
#   lab/kata.sh check            a pod under kata-qemu-runtime-rs: its kernel must differ from the node's
#   lab/kata.sh uninstall
#
# kata-deploy restarts k3s on the node it installs to (to load containerd's new runtime): pick a
# node without a cell. Pods asking for RuntimeClass kata-clh are scheduled only onto it (the
# RuntimeClass carries kata-deploy's node label). The Rust runtime (runtime-rs) is Kata's default
# since 4.0; the Go runtime's shims (kata-clh, kata-qemu) are deprecated.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE="${ROOT}/.netci-gate/lab"
export KUBECONFIG="${STATE}/kubeconfig"
CHART=oci://ghcr.io/kata-containers/kata-deploy-charts/kata-deploy VERSION=4.2.0

case "${1:-}" in
  install)
    node="${2:-netci-lab-3}"
    kubectl label node "${node}" netci.io/kata=true --overwrite >/dev/null
    mkdir -m 700 -p "${STATE}/kata"
    cat > "${STATE}/kata/values.yaml" <<'VALUES'
k8sDistribution: k3s
nodeSelector: {netci.io/kata: "true"}
defaultShim: {amd64: qemu-runtime-rs}
shims:
  disableAll: true
  qemu-runtime-rs:
    enabled: true
    # Kata's default guest memory, 2048 MiB, does not fit the lab's 5 GB machines. With a memory
    # limit set (the fabric always sets one) the VM is sized by the limit, not by this plus the
    # limit: measured, a 512 MiB / 1 GiB / 2 GiB limit gave a guest of 443 / 947 / 1953 MiB with
    # this at 256 or at 512. The guest's kernel takes 70-95 MiB of the sandbox's memory.
    dropIn: |
      [hypervisor.qemu]
      default_memory = 256
      default_vcpus = 1
VALUES
    helm upgrade --install kata-deploy "${CHART}" --version "${VERSION}" -n kube-system \
      -f "${STATE}/kata/values.yaml" --wait --timeout 15m
    kubectl get runtimeclass ;;
  check)
    kubectl delete pod kata-check -n netci-agents --ignore-not-found >/dev/null
    kubectl run kata-check -n netci-agents --restart=Never --image=172.17.0.1:8930/mirror/jenkins/inbound-agent:3386.v353e57a_1b_ea_0-1-jdk21 \
      --overrides='{"spec":{"runtimeClassName":"kata-qemu-runtime-rs","imagePullSecrets":[{"name":"harbor-pull"}],"containers":[{"name":"c","image":"172.17.0.1:8930/mirror/jenkins/inbound-agent:3386.v353e57a_1b_ea_0-1-jdk21","command":["sh","-c","uname -r; grep -c ^processor /proc/cpuinfo; grep MemTotal /proc/meminfo; grep -m1 -o hypervisor /proc/cpuinfo; sleep 5"],"resources":{"limits":{"cpu":"1","memory":"512Mi"}}}]}}' >/dev/null
    kubectl wait -n netci-agents pod/kata-check --for=jsonpath='{.status.phase}'=Succeeded --timeout=300s >/dev/null
    node="$(kubectl get pod -n netci-agents kata-check -o jsonpath='{.spec.nodeName}')"
    echo "pod on ${node}; inside the sandbox:"; kubectl logs -n netci-agents kata-check | sed 's/^/  /'
    echo "the node's kernel: $(kubectl get node "${node}" -o jsonpath='{.status.nodeInfo.kernelVersion}')"
    kubectl delete pod kata-check -n netci-agents >/dev/null ;;
  uninstall)
    helm uninstall kata-deploy -n kube-system || true
    kubectl label nodes --all netci.io/kata- >/dev/null ;;
  *) sed -n '2,11p' "$0" >&2; exit 2 ;;
esac
