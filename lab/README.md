# Lab: three KVM guests, k3s, Longhorn

The lab stands for the organisation's VM fleet (ADR-060/061). Controllers (cells) and build
sandboxes run on the same three guests; a machine loss is a guest powered off through libvirt,
not a container killed.

```bash
lab/vms.sh up          # 3 guests: 4 vCPU, 5 GiB, 40 GiB thin disk, fixed addresses .211-.213
lab/k3s.sh up          # k3s v1.36.4 (3 servers, embedded etcd) + Longhorn v1.13.0 (3 replicas)
lab/spike/deploy.sh    # spike cell: one Jenkins controller on a Longhorn volume
export KUBECONFIG=$(lab/k3s.sh kubeconfig)
```

Everything secret (SSH key, k3s token, cell admin password, registry pull config) is generated
into `.netci-gate/lab/` and never committed. The guests pull images from the lab Harbor
(`172.17.0.1:8930`, lab CA trusted by containerd); the controller is reachable from the host on
any node at `:30080`.

## Spike probes

```bash
lab/spike/probe.py resume --failure jvm-kill|pod-delete|node-poweroff --seconds 240
lab/spike/probe.py queue  --failure jvm-kill
```

Each run writes `lab/evidence/spike-*.json`: timings from the failure (detected, fenced, Jenkins
up, build resumed), where the controller and the agent ran, the build result, and which log
lines are missing. `node-poweroff` destroys the guest running the controller, waits for
`NotReady`, confirms the guest is off, applies the `out-of-service` taint (fencing), measures,
then powers the guest back on and removes the taint.

## Requirements

`/dev/kvm` with nested virtualisation (`/sys/module/kvm_intel/parameters/nested` = `Y`) for Kata
sandboxes, libvirt's `default` network, passwordless sudo for the disk directory
`/var/lib/libvirt/images/netci-lab`, and the Ubuntu 24.04 cloud image
(`NETCI_LAB_BASE_IMAGE`, default `~/iso/noble-server-cloudimg-amd64.img`).
