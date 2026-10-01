#!/usr/bin/env bash
# The lab's VM fleet (ADR-060/061): three KVM guests that run k3s with embedded etcd and
# Longhorn. They stand for the organisation's VMs: controllers (cells) and build sandboxes both
# run on them, and failover is tested by powering a guest off through libvirt -- a real
# machine loss, not a container kill.
#
#   lab/vms.sh up       create or start what is missing (idempotent)
#   lab/vms.sh status   state and address of each guest
#   lab/vms.sh ssh N    shell on guest N
#   lab/vms.sh down     shut the guests down (disks kept)
#   lab/vms.sh destroy  delete the guests and their disks
#   lab/vms.sh reclaim  give the host back the space the guests freed, one guest at a time
#
# Addresses are fixed by DHCP reservation on libvirt's default network, so a restart does not
# move a node (the corp lab lost etcd quorum to exactly that, 2026-09-29).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE="${ROOT}/.netci-gate/lab"
DISKS=/var/lib/libvirt/images/netci-lab
BASE_IMAGE="${NETCI_LAB_BASE_IMAGE:-${HOME}/iso/noble-server-cloudimg-amd64.img}"
VIRSH=(virsh -c qemu:///system)
NODES=3
VCPUS="${NETCI_LAB_VCPUS:-4}"
MEMORY_MB="${NETCI_LAB_MEMORY_MB:-5120}"
DISK_GB="${NETCI_LAB_DISK_GB:-40}"
log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }

name() { echo "netci-lab-$1"; }
mac() { printf '52:54:00:4e:43:%02x' "$((16 + $1))"; }
ip() { echo "192.168.122.$((210 + $1))"; }

ssh_key() {
  mkdir -p "${STATE}/ssh" && chmod 700 "${STATE}" "${STATE}/ssh"
  [[ -f "${STATE}/ssh/id_ed25519" ]] || ssh-keygen -q -t ed25519 -N "" -C netci-lab -f "${STATE}/ssh/id_ed25519"
}

reserve_address() {  # a DHCP host entry per guest; added once, left alone after
  local i=$1
  if ! "${VIRSH[@]}" net-dumpxml default | grep -q "$(mac "$i")"; then
    "${VIRSH[@]}" net-update default add ip-dhcp-host \
      "<host mac='$(mac "$i")' name='$(name "$i")' ip='$(ip "$i")'/>" --live --config >/dev/null
  fi
}

seed() {  # cloud-init: the netci user with the lab key, the packages Longhorn needs
  local i=$1 dir="${STATE}/seed-$i"
  mkdir -p "${dir}"
  cat > "${dir}/user-data" <<EOF
#cloud-config
hostname: $(name "$i")
users:
  - name: netci
    groups: [sudo]
    shell: /bin/bash
    sudo: "ALL=(ALL) NOPASSWD:ALL"
    ssh_authorized_keys:
      - $(cat "${STATE}/ssh/id_ed25519.pub")
package_update: true
packages: [open-iscsi, nfs-common, qemu-guest-agent, jq]
runcmd:
  - systemctl enable --now iscsid qemu-guest-agent
  - modprobe iscsi_tcp && echo iscsi_tcp > /etc/modules-load.d/iscsi_tcp.conf
  # Longhorn and the build sandboxes both need many inotify instances; the default 128
  # crash-loops pods (seen on the corp lab).
  - printf 'fs.inotify.max_user_instances=1024\nfs.inotify.max_user_watches=524288\n' > /etc/sysctl.d/90-netci.conf
  # Page cache written back within ~1 s: a node that loses power loses at most that much of a
  # controller's state (spike 2026-10-01: at the 30 s default a pipeline went back ~20 s and a
  # step ran twice; synchronous mounts fixed it at ~4x the cost per build, this at ~none).
  - printf 'vm.dirty_expire_centisecs=100\nvm.dirty_writeback_centisecs=100\n' > /etc/sysctl.d/91-netci-writeback.conf
  - sysctl --system
EOF
  printf 'instance-id: %s\nlocal-hostname: %s\n' "$(name "$i")" "$(name "$i")" > "${dir}/meta-data"
  cloud-localds "${dir}/seed.iso" "${dir}/user-data" "${dir}/meta-data"
  sudo install -m 644 "${dir}/seed.iso" "${DISKS}/$(name "$i")-seed.iso"
}

up() {
  [[ -r "${BASE_IMAGE}" ]] || { echo "base image not found: ${BASE_IMAGE}" >&2; exit 1; }
  ssh_key
  sudo mkdir -p "${DISKS}"
  # A private copy: the guests' disks are overlays on it, and the file they point at must not
  # change underneath them.
  [[ -f "${DISKS}/base-noble.qcow2" ]] || sudo install -m 644 "${BASE_IMAGE}" "${DISKS}/base-noble.qcow2"
  for i in $(seq 1 "${NODES}"); do
    local n; n=$(name "$i")
    reserve_address "$i"
    if ! "${VIRSH[@]}" dominfo "$n" >/dev/null 2>&1; then
      log "creating $n ($(ip "$i"), ${VCPUS} vCPU, ${MEMORY_MB} MiB, ${DISK_GB} GiB)"
      sudo qemu-img create -q -f qcow2 -F qcow2 -b "${DISKS}/base-noble.qcow2" "${DISKS}/$n.qcow2" "${DISK_GB}G"
      seed "$i"
      # host-passthrough: the guests expose VMX/SVM, so Kata sandboxes can run inside them.
      virt-install --connect qemu:///system --name "$n" --vcpus "${VCPUS}" --memory "${MEMORY_MB}" \
        --cpu host-passthrough --os-variant ubuntu24.04 --import \
        --disk "path=${DISKS}/$n.qcow2,bus=virtio,discard=unmap" --disk "path=${DISKS}/$n-seed.iso,device=cdrom" \
        --network "network=default,mac=$(mac "$i"),model=virtio" \
        --graphics none --noautoconsole --autostart >/dev/null
    elif [[ "$("${VIRSH[@]}" domstate "$n")" != running ]]; then
      log "starting $n"; "${VIRSH[@]}" start "$n" >/dev/null
    fi
  done
  for i in $(seq 1 "${NODES}"); do
    for _ in $(seq 90); do
      ssh_run "$i" 'cloud-init status --wait >/dev/null 2>&1; test -f /var/lib/cloud/instance/boot-finished' 2>/dev/null && break
      sleep 5
    done
    log "$(name "$i") ready at $(ip "$i")"
  done
}

ssh_run() {
  local i=$1; shift
  ssh -i "${STATE}/ssh/id_ed25519" -o StrictHostKeyChecking=accept-new \
    -o UserKnownHostsFile="${STATE}/ssh/known_hosts" -o ConnectTimeout=5 -o BatchMode=yes \
    "netci@$(ip "$i")" "$@"
}

status() {
  for i in $(seq 1 "${NODES}"); do
    printf '%s\t%s\t%s\n' "$(name "$i")" "$(ip "$i")" "$("${VIRSH[@]}" domstate "$(name "$i")" 2>/dev/null || echo absent)"
  done
}

down() { for i in $(seq 1 "${NODES}"); do "${VIRSH[@]}" shutdown "$(name "$i")" >/dev/null 2>&1 || true; done; }

destroy() {
  for i in $(seq 1 "${NODES}"); do
    local n; n=$(name "$i")
    "${VIRSH[@]}" destroy "$n" >/dev/null 2>&1 || true
    "${VIRSH[@]}" undefine "$n" >/dev/null 2>&1 || true
    sudo rm -f "${DISKS}/$n.qcow2" "${DISKS}/$n-seed.iso"
  done
}

# Guests' disks are thin qcow2 files that only grow: Longhorn rebuilding replicas after every
# power-off kept writing new blocks, and the host's disk filled until QEMU paused two guests
# (I/O error policy: stop). With discard, blocks a guest frees are freed in the file too.
# Turning it on for an existing guest needs a cold restart, so each guest is drained (its cell
# moves the ordinary way), restarted, trimmed, and the next waits until Longhorn is healthy.
reclaim() {
  export KUBECONFIG="${STATE}/kubeconfig"
  for i in $(seq 1 "${NODES}"); do
    local n; n=$(name "$i")
    # The running definition decides: a guest defined with discard but not yet restarted (an
    # interrupted run) still writes without it.
    if ! "${VIRSH[@]}" dumpxml "$n" | grep -q "discard='unmap'"; then
      if ! "${VIRSH[@]}" dumpxml --inactive "$n" | grep -q "discard='unmap'"; then
        "${VIRSH[@]}" dumpxml --inactive "$n" \
          | sed "s|<driver name='qemu' type='qcow2'/>|<driver name='qemu' type='qcow2' discard='unmap' detect_zeroes='unmap'/>|" \
          > "${STATE}/$n.xml"
        "${VIRSH[@]}" define "${STATE}/$n.xml" >/dev/null
      fi
      log "$n: drain"
      # --force: sandbox pods have no controller (netci-fabric owns them through its database),
      # and the fabric replaces one that disappears.
      kubectl drain "$n" --ignore-daemonsets --delete-emptydir-data --force --timeout=600s >/dev/null
      "${VIRSH[@]}" shutdown "$n" >/dev/null
      until [[ "$("${VIRSH[@]}" domstate "$n")" == "shut off" ]]; do sleep 2; done
      "${VIRSH[@]}" start "$n" >/dev/null
      until kubectl get node "$n" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null | grep -q True; do sleep 3; done
      kubectl uncordon "$n" >/dev/null
    fi
    log "$n: trim $(ssh_run "$i" 'sudo fstrim -av' | tr '\n' ' ')"
    until [[ "$(kubectl -n longhorn-system get volumes.longhorn.io -o jsonpath='{.items[*].status.robustness}')" =~ ^(healthy ?)+$ ]]; do sleep 5; done
  done
  sudo du -sh "${DISKS}"
}

case "${1:-}" in
  up) up ;;
  reclaim) reclaim ;;
  status) status ;;
  ssh) shift; i=$1; shift; ssh_run "$i" "$@" ;;
  down) down ;;
  destroy) destroy ;;
  *) sed -n '2,16p' "$0" >&2; exit 2 ;;
esac
