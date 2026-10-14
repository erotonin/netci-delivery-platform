# ADR-084: Active-active replicas are fenced by power, on OpenStack through Nova

- Status: accepted; unit-tested, measured in the lab (below)
- Date: 2026-10-14
- Refines: ADR-060 (the supervisor fences cells), ADR-079 (adoption needs the runner gone)

## Context

A replica adopts another's running build only once its fence says the runner is gone (ADR-079).
The lab's fence read Kubernetes: a missing or deleted pod, or a node not Ready, meant gone. That
holds for a machine that lost power. It does not hold for one cut off from the network, which keeps
running its pod while the API shows the node NotReady and, later, the pod deleted. Adopting then
runs the build twice. The cells never had this problem: the supervisor powers a cell's machine off
and confirms it off before anything takes over. The replicas had no cell Lease for it to act on.

At the site the controllers are OpenStack VMs, and the owner answered that the cluster may hold an
application credential allowed to stop and start them.

## Decision

1. **A Nova fencer** (`internal/fence/nova.go`):
   - Keystone application-credential login, token reused until a minute before it expires;
   - the compute endpoint from the catalog (interface, region);
   - stop and start by `os-stop` / `os-start`.
   - Off is only SHUTOFF with power state SHUTDOWN and no task in progress. Anything else is
     Unknown, which never counts as off.
   - Keystone's 401 is a refusal. Nova's 401 means log in again. Nova's 409 means busy, so ask again.
   - The secret is never in an error.
   - Configured per node in the fence config:
     `{"nova": {"authURL", "region", "server", "credentials"}}`.
   - The controllers' image should carry `os_shutdown_timeout=0`, so that the stop is immediate.
2. **The supervisor fences a lost node that has no cell** when the node is labelled
   `netci.io/fence-on-loss=true` (`FenceLostNode`):
   - the trigger is a kubelet silent for `NETCI_NODE_FENCE_AFTER` (45 s) and the node not Ready;
   - the machine is powered off, or confirmed off, through `EnsureOff`, and only then the node gets
     the out-of-service taint;
   - the cells' guards apply: nothing is powered off while failures look shared, at most one
     running machine per decision, an unreadable power state is an alert;
   - `recovery()` removes the taint when the machine is back.
3. **The replicas' fence has a strict mode**, `NETCI_ADOPT_FENCE=kubernetes-power`, for production.
   A runner counts as gone only on proof that it cannot write:
   - its node carries the out-of-service taint;
   - or the kubelet of a node that is Ready reports the old process ended (container stopped, or a
     newer one).

   A missing or deleted pod and a NotReady node are not enough. `kubernetes` (lenient) stays for
   the lab's quick runs.

## Tests

- Nova, against a fake Keystone and Nova:
  - fenced end to end with one login;
  - Off only when the hypervisor has stopped the server (five in-between states are Unknown);
  - refusals vs retries;
  - a server id that is a path refused;
  - fence-config parsing, including two nodes on one server refused.
- Supervisor:
  - a labelled lost node is fenced, powered off or confirmed off;
  - an unreadable state is an alert;
  - not labelled, still Ready, silent briefly, or already fenced: left alone;
  - two lost of three: nothing powered off;
  - zero disables it.
- Plugin (`FenceTest`):
  - a partitioned machine is gone to the lenient fence and not to the strict one, until the taint;
  - a reporting kubelet proves a new or ended process in both modes;
  - a missing pod is gone only to the lenient fence.

## Lab: a partition (`lab/aa/phase8_partition_probe.py`, `aa-phase8-partition-20261014T171635Z.json`)

The setup:
- the replicas on netci29 with `NETCI_ADOPT_FENCE=kubernetes-power`;
- the supervisor on lost-node fencing, through the lab's Redfish emulator;
- `long` on agent-0, owned by r0.

At tick 10, r0's machine (netci-lab-2) lost its link (`virsh domif-setlink ... down`): the VM kept
running, and its Jenkins with it. Seconds after the link went down:
- 51.7: the node NotReady;
- 51.6: the supervisor's out-of-service taint, put on after its power controller confirmed the
  machine off (the probe, polling, saw the VM off at 54.0);
- **57.8: jenkins-1 adopted the build, after the fence and not before**;
- 81.9: the build ended SUCCESS, 90 of 90 ticks, none twice, resumed once.

The lenient fence would have adopted at the NotReady with the old replica still running. Here it
happened to come at the same second, which is why only the power-off may decide.

## What it costs, said plainly

- With the strict fence, a lost replica's builds are adopted after the supervisor's fence: 45 s of
  silence, then the power-off and its confirmation. That is longer than the lenient fence's ~50 s
  NotReady, but it is the one a partition cannot fool.
- A VM whose hypervisor host died cannot be stopped by Nova, which keeps reporting its last state.
  It stays unfenced, and its builds unadopted, until an operator forces the compute service down.
  That is a stall, not a double run.
- The application credential is a Secret in the cluster that can stop the controllers' VMs.
  Restrict it to them (an application credential with access rules, or a project holding only
  them).
