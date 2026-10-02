# How netCI's takeover compares, and what it rests on

netCI keeps one Jenkins controller per cell. When the controller's machine is lost, it starts
the controller elsewhere on the same replicated `JENKINS_HOME`. This page sets that against
the products that solve neighbouring problems, and the published work the design relies on.
Claims about netCI cite its lab evidence (ADR-060); claims about others cite their own
documentation, and nothing here is a measurement of them.

## Products

| | netCI | CloudBees CI HA (active/active) | Medik8s NHC + Fence Agents Remediation |
|---|---|---|---|
| What it protects | One Jenkins controller per cell, for whole-machine loss | A controller run as several replicas sharing one `JENKINS_HOME` | Any workload, by remediating unhealthy nodes |
| Shared state | `JENKINS_HOME` on a replicated block volume (RWO), one writer at a time | `JENKINS_HOME` on a shared file system (NFS) that every replica writes [1] | Not its concern |
| Detection | The cell's own Lease: a controller that misses 3 s of renewals gets its machine's power state queried | Replica health inside the product [1] | Node conditions, by NHC; FAR runs after NHC creates a remediation [3] |
| Fencing | Machine confirmed **off** by its power controller (Redfish, SSH agent for libvirt) before anything is released; Lease released conditionally on its version; then the out-of-service taint | Not described in the documentation [2] | Fence agent called (reboot or off), then the out-of-service taint or pod deletion; success taken from the agent's own result [4] |
| Wrong node-to-machine mapping | Checked at start against the kubelet's heartbeat; a Lease renewed after "off" stops the fencing | — | — |
| Running builds | Pipeline durability: builds resume on the new controller; `netciOnce` keeps a marked step from running twice | Running Pipeline builds are adopted by another replica [1] | Not its concern |
| Plugins | Unchanged Jenkins; one controller at a time, so plugins see a normal Jenkins | Several plugins limited or unsupported in HA (Docker, EC2, Kubernetes plugin settings, Blue Ocean, file parameters) [2] | — |
| Published failover time | Lab: fenced in 2.7-3.8 s, build resumed after 41-50 s (6 runs, `chaos-poweroff-20261001T220424Z.json`) | None found [2] | Configurable timeouts; none published for a takeover |

Where netCI is weaker. CloudBees's active/active keeps the controller serving during a
replica's loss: its UI and API do not go down. netCI's cell is unavailable for about 40 s,
and its history and logs cannot be read while the cell is down. The durable run queue keeps
triggers that arrive meanwhile. netCI has been measured only on a three-VM lab, and every
number above is from that lab.

## Published work the design follows

- **Leases** [5]. A holder that cannot renew stops acting before anyone else may act. The cell
  agent kills Jenkins within its renew deadline (10 s), before the 15 s Lease can expire. The
  supervisor releases a Lease only conditionally, on the version it observed.
- **Fencing before reuse** [6][7]. A lock is not enough when a paused or partitioned holder can
  still write. Pacemaker requires STONITH (power fencing) for shared storage, and Kleppmann's
  point is the same: a slow holder must be stopped by something other than itself. netCI
  stops it twice:
  - the agent's guard kills the JVM when it can no longer prove it holds the Lease;
  - the supervisor acts only on a machine its power controller reports **off**.
  The Lease's transition count serves as the fencing epoch. The storage adds a third barrier:
  an RWO volume is attached to one node at a time.
- **Kubernetes non-graceful node shutdown** [8]. The `node.kubernetes.io/out-of-service` taint
  force-deletes pods and detaches volumes without waiting for the kubelet. The KEP requires
  that the node really be shut down, which is exactly what the power controller confirms first.
- **Failure detectors trade speed against mistakes** [9][10]. A faster timeout means more
  false suspicions. netCI separates the two decisions:
  - suspicion is cheap and fast (3 s of missed renewals) and only prompts a question;
  - the decision rests on the power controller's answer, which is not a guess;
  - a machine that is running but silent is powered off only after its kubelet has been
    silent for 20 s, and never when that would cost the control plane its majority or affect
    more than half the cells.
- **Chubby** [11]. Long leases, jeopardy and grace periods for the master, and sequencers
  checked by servers. netCI's equivalents are the renew deadline that outlasts an etcd election
  (a 4 s deadline did not, and a healthy cell restarted once), and the Lease epoch.

## What this changes in netCI's plans

- **Keep confirming off before acting.** FAR acts on the fence agent's success [4]. netCI
  asks for the power state again and releases the Lease conditionally on its version; that
  check found a wrong mapping in a test.
- **The unavailability window is the next limit.** CloudBees reaches zero downtime only with
  replicas that share a file system [1]. netCI's equivalent would be a read-only path for
  history and logs while a cell is taken over.
- **Validate real power controllers.** The Redfish client is tested against a fake server.
  Running it against a Redfish emulator in front of the lab's VMs (sushy-tools) is the closest
  test short of hardware.

## References

1. CloudBees, "High Availability (active/active)", docs.cloudbees.com/docs/cloudbees-ci/latest/ha/install-ha-on-platforms
2. CloudBees, "High Availability (HA) considerations", docs.cloudbees.com/docs/cloudbees-ci/latest/ha/ha-considerations
3. Medik8s, "Node HealthCheck Operator", github.com/medik8s/node-healthcheck-operator
4. Medik8s, "Fence Agents Remediation", github.com/medik8s/fence-agents-remediation
5. C. Gray, D. Cheriton, "Leases: An Efficient Fault-Tolerant Mechanism for Distributed File Cache Consistency", SOSP 1989
6. ClusterLabs, "Pacemaker Explained: Fencing", clusterlabs.org/pacemaker/doc
7. M. Kleppmann, "How to do distributed locking", 2016, martin.kleppmann.com
8. Kubernetes KEP-2268, "Non-graceful node shutdown" (GA in 1.28), github.com/kubernetes/enhancements
9. W. Chen, S. Toueg, M. K. Aguilera, "On the Quality of Service of Failure Detectors", IEEE Transactions on Computers, 2002
10. N. Hayashibara, X. Défago, R. Yared, T. Katayama, "The φ Accrual Failure Detector", SRDS 2004
11. M. Burrows, "The Chubby lock service for loosely-coupled distributed systems", OSDI 2006
