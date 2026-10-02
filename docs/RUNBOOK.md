# netCI runbook

What each alert means, how to confirm it, and what to do. Everything the supervisor does is
recorded as a Kubernetes Event:

```bash
kubectl get events -A --field-selector source=netci-supervisor --sort-by=.lastTimestamp
```

The supervisor's decisions are in ADR-060 and ADR-066. The rule behind every action below is
the supervisor's own: **nothing is fenced on a guess**. A machine is fenced only when its power
controller reports it off. When the supervisor cannot tell, it raises an alert and waits for a
person.

## Alerts

### NetciCellDown (critical)
A cell's controller has not renewed its Lease for two minutes, and the supervisor has not taken
it over.
- **Check:** `kubectl -n <cell> get lease jenkins -o yaml` (holder, renewTime) and the
  supervisor's events for that cell. A `SupervisorAlert` names the reason.
- **Act:** follow the reason. "power state unknown" means the power controller did not answer.
  Check it (the BMC, or the SSH agent), confirm by hand that the machine is off, then follow
  *Fence a machine by hand*. Never release the Lease of a machine that may be running: two
  controllers would write one `JENKINS_HOME`.

### NetciSupervisorNeedsAPerson (warning)
The supervisor decided not to act on its own. The possible reasons:
- an unknown power state;
- the cells of more than half the machines that run cells, or more than half the nodes' kubelets,
  lost at once (a cluster problem, not a machine; cells sharing one machine count once);
- the control plane without a majority;
- a cell failing again within its 2-minute cooldown.
- **Check:** the `SupervisorAlert` events give the exact reason.
- **Act:** a mass failure or a lost majority means fix the cluster first; the supervisor acts
  again on its own once it can observe a consistent picture. For a repeated failure within the
  cooldown, look at why the controller keeps dying: its log, OOM kills, the node.

### NetciFenceLeaseMoved (critical)
A fencing was stopped because the cell's Lease was renewed after its machine had been
confirmed off. The node-to-machine mapping is wrong: the machine that was asked is not the one
that runs the node.
- **Act:** fix `NETCI_MACHINES` or the fence configuration before the next failure. The
  supervisor checks the mapping at start (`node-to-machine mapping checked` in its log). A
  mapping that is wrong anyway means a machine was renamed or moved since then.

### NetciSupervisorNotLeading (critical)
No replica is leading, or none is running at all, so a controller that dies now is not taken
over.
- **Check:** `kubectl -n netci-system get pods -l app=netci-supervisor` and their logs. A
  replica that cannot reach the API server, or that failed its start-up check of the mapping,
  does not lead.

### NetciCellConfigurationRefused (critical)
A cell's controller was asked to apply its changed JCasC ConfigMap and refused it. It runs its
previous configuration, possibly in part changed (JCasC is not transactional), and **would not
start on this file**: the next restart or takeover of that cell would leave it down.
- **Check:** the controller's log, "Failed to reload Jenkins Configuration as Code via token",
  gives the reason; the cell agent logs only that it was refused (the reason can quote values).
- **Act:** fix the ConfigMap now. The agent applies the next change by itself.

### NetciComponentDown (critical)
No replica of netci-queue (`component` label) or netci-fabric has answered Prometheus for five
minutes. Without the queue, webhooks and triggers are refused and nothing is dispatched; without
the fabric, builds that need a sandbox wait.
- **Check:** `kubectl -n netci-system get pods -l app=<component>` and their logs; a replica that
  cannot reach PostgreSQL fails its readiness check.

### NetciSupervisorBlind (warning)
The supervisor's observations keep failing, and it decides nothing while it cannot observe.
- **Check:** the API servers' health. If the API servers are slow to answer the supervisor's
  lists, raise `NETCI_API_ATTEMPT_TIMEOUT` (default 0.7 s).

### NetciCellWithoutHeadroom (warning)
If the machine running this cell's controller were lost, no other machine could take it, even
with every build evicted. A takeover would fence the machine and then wait. The event
`NoTakeoverHeadroom` names each machine and why it cannot take the controller: cordoned, not
Ready, a taint, or the room left.
- **Act:** add a machine or memory, or move pods of the same or higher priority off one machine.
  This check is a necessary condition. When it passes, it does not promise that a takeover will
  succeed.

### NetciCellsShareAMachine (info)
Two or more cells' controllers are on one machine and would be lost together. Takeovers can
gather cells, because they spread by preference, not by rule.
- **Act:** at a quiet time, `kubectl -n <cell> delete pod jenkins-0`. This restarts that
  controller (about 30 s; running builds resume), and it prefers a machine without a cell.

### A controller restarted for its JENKINS_HOME (`PodRestarted` event)
The cell agent could not write to `JENKINS_HOME` three times in a row, and the supervisor
restarted the pod so that the volume would be mounted again (ADR-067). The event gives the error.
- **Check:** the storage behind the volume, for example Longhorn's volume events or the NFS
  server. A second failure within the cooldown raises `NetciSupervisorNeedsAPerson` instead of
  another restart.

### NetciTakeoverSlow (warning)
Takeovers took more than 2 minutes at p95 over a day. The time from the last renewal to the
Lease held again is in `TakeoverComplete` events.
- **Check, in order:**
  - the time to fencing: the supervisor's leader on the lost machine, or observations failing;
  - the volume attaching elsewhere: the storage. For Longhorn, run `lab/longhorn-tune.sh` again
    after an upgrade;
  - the controller's start: plugins, JCasC.

### NetciControllerUnreachable (warning) / NetciRunsLostWithAController (info)
netci-queue cannot hand runs to a cell, so they wait in PostgreSQL, not lost. The second alert
means a controller lost runs it had queued (a restart), and they were handed over again.

### NetciFabricNoWarmSandbox / NetciFabricSandboxesFailing (warning)
A pool has no warm sandbox, so builds wait for cold ones; or its sandboxes keep failing.
- **Check:** `kubectl -n netci-agents get pods` and their events: image pulls, scheduling, limits.

## Procedures

### Fence a machine by hand
Only when the machine is **confirmed off**: through its BMC or hypervisor, or physically.

```bash
kubectl taint node <node> node.kubernetes.io/out-of-service=nodeshutdown:NoExecute
kubectl -n <cell> delete pod jenkins-0 --force --grace-period=0
```

When the machine is back and its node is Ready, remove the taint:
`kubectl taint node <node> node.kubernetes.io/out-of-service-`. The supervisor removes only
the taints it added itself, which carry the `netci.io/fenced-by` annotation.

### Drain a machine for maintenance
`kubectl drain <node> --ignore-daemonsets --delete-emptydir-data --force`.
- `--force` is for build sandboxes, which have no controller of their own; netci-fabric
  replaces them.
- A cell's controller moves the ordinary way, and its builds resume.
- Longhorn holds the drain while the machine has a volume's last healthy replica.

### Change a cell's configuration (JCasC)
Update its ConfigMap. With `casc-reload-token` in the cell's Secret, the cell agent applies the
change in place within about a minute and logs `the controller applied it in place`; no restart.
Without it, the change applies at the next start.

### Revoke the lab's power key
Remove the line ending in `netci-fence` from `~/.ssh/authorized_keys` on the hypervisor host.

### Failure drills
Before anyone depends on a cell, run `lab/spike/chaos_poweroff.py` on the target
infrastructure. It powers off the machine of a running build and checks:
- the build finished;
- each step ran once;
- a `netciOnce` block started once;
- the other cell was untouched;
- the machine came back.
