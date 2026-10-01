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

## Cell agent probes

```bash
NETCI_CELL=cell-b lab/spike/agent_probe.py handover|agent-restart|agent-hang|partition
```

Each one exercises one way the cell agent stops a controller that lost its Lease, on the real
cluster, and writes `lab/evidence/agent-*.json`. `partition` drops the pod's traffic to the API
server with iptables inside the pod's network namespace (on the node, via SSH), so the kubelet
keeps working; it removes the rule afterwards.

## Cell Supervisor

```bash
NETCI_IMAGE=172.17.0.1:8930/netci/netci@sha256:<digest> lab/supervisor.sh
```

It installs the supervisor in `netci-system` with this host as the power controller. **It
changes this host:** it adds one line to `~/.ssh/authorized_keys`. That line is for a key
generated into `.netci-gate/lab/fence/` and is restricted:
- `restrict`;
- `from=` the three guests only;
- a forced command, `~/.local/libexec/netci-fence` (a copy of `lab/fence/netci-fence`), which
  can only read, stop or start guests named `netci-lab-<n>`.

To revoke it, delete the line ending in `netci-fence`.

## Run queue

```bash
NETCI_IMAGE=172.17.0.1:8930/netci/netci@sha256:<digest> lab/queue.sh
lab/spike/queue_crash_probe.py --runs 5
```

`lab/queue.sh` installs PostgreSQL (one instance, lab only) and two `netci-queue` replicas in
`netci-system`, with intake on NodePort `30090`. Its secrets are under `.netci-gate/lab/queue/`:
- the database password;
- the API token of cell-b's `netci` user;
- the probe's client token (the configuration holds only its SHA-256).

The probe crashes the controller under queued runs and checks Jenkins' own build records.

**Known Jenkins behaviour:** a job that JCasC's Job DSL creates while the controller starts can
be overwritten by the concurrent "Loaded all jobs" phase. It is then on disk but absent until
`/reload`. Jobs belong in a seed job run after start-up, not in JCasC.

## Requirements

`/dev/kvm` with nested virtualisation (`/sys/module/kvm_intel/parameters/nested` = `Y`) for Kata
sandboxes, libvirt's `default` network, passwordless sudo for the disk directory
`/var/lib/libvirt/images/netci-lab`, and the Ubuntu 24.04 cloud image
(`NETCI_LAB_BASE_IMAGE`, default `~/iso/noble-server-cloudimg-amd64.img`).
