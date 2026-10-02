# ADR-061: Agent fabric -- isolated build sandboxes on VM hosts, provisioned ahead of demand

Status: Proposed (2026-10-01).

## Context

The organisation builds on long-lived VM agents: state leaks from one build into the next,
idle VMs cost capacity, and peaks queue. One fresh VM per build fixes the first problem and
wastes the machine: most builds need a fraction of a VM, and a VM takes minutes to boot.

The practice elsewhere (verified, sources in the links at the end): GitLab runs several jobs
per autoscaled instance (`capacity_per_instance`) and warns that jobs sharing an instance
must be trusted, recommending a job-per-instance or a nested VM for untrusted ones; Nodepool
and GitLab keep ready capacity (`min-ready`, `idle_count`) and retire instances after N uses
(`max_use_count`); microVM runners (Firecracker) boot a fresh guest per job in under a second;
Jenkins' default provisioning deliberately waits before adding capacity, which the EC2 Fleet
plugin's no-delay strategy removes.

## Decision

1. **A VM is a host; a build runs in a sandbox on it.** The VM fleet is a Kubernetes cluster
   (k3s/RKE2 on the organisation's hypervisor). A sandbox is a pod holding one Jenkins agent
   with one executor, created for one build and destroyed after it. Kubernetes supplies
   scheduling by resources, cgroup limits and networking; netCI supplies the policy.

2. **Isolation follows trust (RuntimeClass).**

   | Class | For | Runtime | Isolation |
   |---|---|---|---|
   | `standard` | the organisation's own branches | runc with a user namespace (`hostUsers: false`), seccomp, no privilege | shared kernel; container root is unprivileged on the host |
   | `docker` | builds that run `docker build` / compose | Sysbox (Docker inside without `--privileged`) | wider kernel surface (+~267 % reachable syscalls): trusted code only |
   | `untrusted` | fork pull requests, outside code | Kata Containers (needs nested virtualisation), else gVisor, else a dedicated host per build | separate kernel |

   Untrusted sandboxes also run on their own node pool (taints), so a kernel escape from a
   trusted-class sandbox and an untrusted build never share a host.

3. **Neighbours cannot starve each other.** Every sandbox has CPU, memory and PID limits,
   an ephemeral-storage limit, IO weight, and a network policy that allows egress only to what
   builds need (git, registry, artifact proxy, the cell). Workspaces are ephemeral. Secrets
   arrive at step time through Jenkins credentials, never in an image.

4. **Hosts are immutable and short-lived.** A host is drained -- no new sandboxes, running ones
   finish -- and replaced when it reaches N builds, M hours, an outdated image (image drift is
   gated like plugin drift, ADR-059) or fails health checks. Images are built with Packer
   from `toolchain/versions.yaml`.

5. **Caches are shared without being poisoned.** Image layers and dependencies come through
   read-only mirrors/proxies on or near each host; writable caches are per tenant, so one
   team's build cannot plant content another team's build will execute.

6. **Two-level scaling, ahead of demand.**
   - *Sandboxes (seconds):* warm sandboxes are started in advance with the toolchain loaded
     and an agent that is not yet bound to any controller. A claim binds one to a cell and a
     build (late binding), so the build starts in about a second instead of paying image pull
     and JVM start.
   - *Hosts (minutes):* the fleet grows when projected use crosses a threshold. The projection
     reads the global queue (ADR-060) -- which knows about builds before any controller does --
     plus the time-of-day history; it shrinks when hosts are idle beyond a hold time, never
     below a floor.
   Both levels have per-class and per-label limits and respect hypervisor quota.

7. **Lifecycle is a state machine in PostgreSQL**, atomic and audited like everything else:
   sandbox `warm -> claimed -> connected -> busy -> released -> deleted`, with `failed` and
   `orphaned`; host `provisioning -> ready -> draining -> retired`, with `unhealthy`.
   Rules that are never broken: a sandbox holding a build is never deleted, including while
   its cell is taking over (ADR-060) -- it is kept until the build resumes or the durable-task
   timeout plus a grace period passes; orphans (a sandbox with no Jenkins node, a node with no
   sandbox) are reaped only after that grace.

8. **Jenkins sees an ordinary cloud.** A Java plugin implements a Jenkins `Cloud` that claims
   sandboxes from the fabric with no provisioning delay, so builds started inside Jenkins --
   by people, cron or other jobs -- are served too, not only those netCI dispatches. One
   fabric serves every cell.

9. **Hosts come from a driver.** libvirt/KVM in the lab first; vSphere, OpenStack or Proxmox
   for the organisation (its hypervisor is not known yet). Cluster API providers are an
   option behind the same interface.

10. **Measured.** Agent wait p50/p95, sandbox start, host boot, utilisation, idle
    sandbox-minutes and host-minutes wasted, failures by class; every policy change is judged
    against these numbers, before and after.

## Rejected

- **A fresh VM per build.** Minutes of boot and a whole VM for a fraction of its use; kept
  only as the last fallback for untrusted builds where nested virtualisation is unavailable.
- **Long-lived shared VM agents** (today). Leaks between builds, no isolation between teams.
- **A scheduler written for this.** Kubernetes already schedules by resources, enforces
  cgroups and networking and offers RuntimeClass; the value to add is the policy on top.
- **The Kubernetes plugin's provisioning alone.** It creates a pod after the build is queued
  (image pull and JVM start on the critical path), has no warm capacity bound late to a
  controller, no trust classes and no fleet scaling ahead of demand.

## Consequences

- The organisation runs Kubernetes on its build VMs; people still see "a VM fleet".
- `untrusted` at full strength needs nested virtualisation on the hypervisor (vSphere and KVM
  offer it). ~~Where it is absent the fabric falls back and reports which fallback it used.~~
  Superseded (2026-10-02): there is no fallback. A pool's RuntimeClass is checked when
  netci-fabric starts, and a missing one stops it with the pool named. A fallback would run code
  the organisation does not trust on the shared kernel while the pool still said `untrusted`.
  An organisation without nested virtualisation gives such builds dedicated hosts or does
  without the pool -- a choice made in the open, not by the fabric.

## Verified on the lab (2026-10-02)

- `standard`: container root maps to an unprivileged host uid (`fabric-agents-*.json`).
- `untrusted`: Kata Containers 4.2.0, QEMU on the Rust runtime, on one machine with nested KVM
  (`lab/kata.sh`). Four builds ran in four VMs: guest kernel 6.18.35 against the machines'
  6.8.0-142, CPU flag `hypervisor`. Each VM was discarded after its build. A build took ~10 s
  with a warm sandbox, as on `standard` (`lab/evidence/untrusted-sandboxes-20261002.json`).
- A Kata VM is sized by the pod's memory limit, and its kernel keeps 70-95 MiB of it: a pool
  limited to 1 GiB gives builds 947 MiB. Kata's `default_memory` does not add to it.
- The NetworkPolicy holds from inside a Kata VM as from a `standard` sandbox: fabric,
  controller and internet reachable; netci-queue, the API servers, a kubelet and Prometheus not.
- A pool naming a RuntimeClass the cluster lacks: the new fabric replica stopped with the pool
  named, and the running replicas kept serving until the change was rolled back.
- Not done: `docker` (Sysbox), a separate node pool with taints for `untrusted`, image builds
  inside an untrusted VM.

Sources: docs.gitlab.com (runner advanced configuration, docker autoscaler, instance
executor), zuul-ci.org nodepool configuration, javadoc ec2-fleet NoDelayProvisionStrategy,
actuated.com, northflank.com and edera.dev isolation comparisons, github.com/nestybox/sysbox,
kubernetes.web.cern.ch (rootless builds, user namespaces default in 1.33).
