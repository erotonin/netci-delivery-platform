# ADR-070: A cell's pod goes after the storage has settled, and never while it renews

- Status: accepted
- Date: 2026-10-09
- Refines: ADR-060 (takeover), ADR-069

## Context

ADR-069 left one thing unverified: storage moving many volumes at once. `lab/spike/many_volumes_probe.py`
measures it on the lab: N cells of the real netci-cell chart (cell agent, guard, Lease,
`longhorn-sync` volume) on one machine, Jenkins replaced by a stand-in that appends an fsynced
sequence number five times a second, the machine powered off, each cell timed from the power loss,
and every volume read back.

Eight volumes moved as fast as one. What was slow was something else: **the cell whose pod went
first**. In every 8-cell run, the pod the supervisor deleted with the fencing (within 0.3 s of
marking the node not ready) was the last to attach, and the single cell of every 1-cell run paid the
same: about 10 s, 5 of 5. Longhorn's logs say why: a detach asked for while Longhorn is still taking
in that the node is gone either times out in its CSI plugin (10 s), or is accepted while Longhorn
tries to stop the engine on the machine that is off and gives up 10 s later. The other seven,
deleted ~2.5 s later (after the fencing's 2 s quiet check of their Leases), detached in 2 s.

Two more things came out on the way:

- **A safety defect.** When fencing stopped because another cell on the machine still renewed its
  Lease (a wrong node-to-machine mapping), the recovery of the fenced node deleted that cell's pod
  one second later anyway: it deleted every cell pod bound to a fenced node whose machine read off.
  `TestAnotherCellWhoseHolderStillRenewsIsLeftAlone` checked only the moment of fencing.
- **Kubernetes deletes the pods too.** The out-of-service taint is `NoExecute`: the taint-eviction
  controller deletes every pod that does not tolerate it at once, and the pod garbage collector
  force-deletes it on its next pass (k3s log: `taint_eviction.go "Deleting pod"`, then
  `gc_controller.go "PodGC is force deleting Pod"`, 0.4 s apart). In one 8-cell run it came first for
  3 pods.

And one waste: every cell on the lost machine got its own FenceNode; the first released every
Lease, the other seven asked the power controller again, in a row (8 cells: 2.5 s; with a BMC
answering in 1.5 s, 12 s).

## Decision

1. **The cells' pods on a fenced node are deleted by its recovery, `supervisor.storageSettle` (3 s)
   after the taint**, not with the fencing. The Leases are still released at fencing, so the
   replacements wait for nothing else. `0` deletes at the next observation.
2. **Only a pod that no longer renews is deleted**: its Lease was released for it, or has expired.
   One that still renews is left, and an alert names it. Each deletion carries the UID observed.
3. **The controller pod tolerates `node.kubernetes.io/out-of-service` for
   `outOfServiceTolerationSeconds` (30)**, so taint eviction does not delete it before the
   supervisor; past that Kubernetes still does, so without a supervisor a takeover is slower, never
   stuck.
4. **One fencing per machine per decision.**

## Verified

| What | Result |
|---|---|
| 8 volumes at once (probe, 2 runs, before) | Ready median 21.2 s, max 30.6 s; every write before the power loss present (last one 0.32-0.49 s before it), no gap, no torn line |
| The first pod's penalty (before) | 5 of 5 pods deleted within 0.3 s of NotReady: ~10 s more; 11 of 14 deleted ≥ 2.2 s after: none |
| After, 1 cell (7 runs) | Every detach in ~2 s but one (1 of 7 still timed out); attached on average 16.0 s after the power loss, 18.9 s before (3 runs) |
| After, 8 cells (3 runs, all four changes) | All 8 pods go together; attached median 15.1 s (14.5 before), Ready median 23.5 s (21.2): no gain with many cells -- they were already deleted late |
| Taint eviction (before the toleration) | 3 of 8 pods deleted by Kubernetes 1.1 s after the taint, two of their volumes late; none after |
| The renewing pod | Kept for 10 s of recovery in the test; failed at 1 s before |
| Real Jenkins, 6 takeovers (chaos 23, 24) | 6/6 SUCCESS, one netciOnce marker each. Fencing to the new pod holding the Lease: median 21.0 s (14.9-41.1), against 22.9 s (13.5-34.5) in the 10 runs before |

**What this does not show**: a shorter takeover end to end. The real-Jenkins difference is inside
the spread, which the lab's other variance dominates -- the controller manager's, scheduler's and
csi-attacher's leaders dying with the machine (the two slowest after, 34.0 and 41.1 s, lost all
three), and Longhorn's own attach: its CSI plugin waits for the engine's endpoint, which the engine
monitor fills on its first pass, every 5 s (`EnginePollInterval`), polled every 2 s; an attach takes
~4 s or ~10 s with nothing else different. The mechanism the settle removes is shown; its weight in
a whole takeover is not.

## Rejected

- **Waiting on a sign from Longhorn** that it has taken in the node loss: storage-specific, and its
  internal states are not an interface.
- **Deleting the pods at once and accepting the 10 s**: it is the commonest failure, one machine with
  one cell, that always paid it.

## Consequences

- With storage that does not need it, a takeover's pod deletion is 3 s later; set
  `supervisor.storageSettle: "0"`.
- A manually added out-of-service taint without a supervisor deletes a controller pod 30 s later
  than before.
- The Longhorn volumes in the lab: 26.4 GiB schedulable per disk; the probe uses 128 MiB volumes and
  refuses when they would not fit.
