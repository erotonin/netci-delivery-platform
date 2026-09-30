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
        --disk "path=${DISKS}/$n.qcow2,bus=virtio" --disk "path=${DISKS}/$n-seed.iso,device=cdrom" \
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

case "${1:-}" in
  up) up ;;
  status) status ;;
  ssh) shift; i=$1; shift; ssh_run "$i" "$@" ;;
  down) down ;;
  destroy) destroy ;;
  *) sed -n '2,16p' "$0" >&2; exit 2 ;;
esac
