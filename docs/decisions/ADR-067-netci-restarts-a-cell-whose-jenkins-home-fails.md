# ADR-067: netCI restarts a cell whose JENKINS_HOME fails; the storage does not

- Status: accepted
- Date: 2026-10-02
- Refines: ADR-060 (cells, takeover), ADR-066 (storage tuning)

## Context

A volume can fail under a pod that keeps running. In the lab, Longhorn's engine for the cell's
volume died and Longhorn attached the volume again. The pod's mount then failed every write.
Kubernetes restarts no pod for that, so the controller would have run on, broken.

Longhorn has its own answer, `auto-delete-pod-when-volume-detached-unexpectedly`. After it
asks for a volume to be remounted, it deletes every pod that uses the volume and started
before that request. A takeover broke this rule. Once fencing had become fast (ADR-060), the
replacement controller often started before Longhorn recorded the remount it was requesting for
the lost machine's attachment. Longhorn then deleted the replacement, with a 30 s grace,
2 s after it had taken its Lease. That happened twice in six power-offs (chaos series 14), and
each time cost 35-55 s. The faster the takeover, the more often it happens.

## Decision

1. **The cell agent probes JENKINS_HOME.**
   - Every 5 s it writes a file under `.netci`, fsyncs it, renames it and fsyncs the directory.
   - A write that has not returned after 10 s counts as a failure, and no second write is
     started while it hangs.
   - After three failures in a row, it reports `"<identity> <time> <error>"` on the cell's Lease
     (`netci.io/volume-failed`, written with its renewals). That needs no permission beyond the
     Lease, and the report is cleared when the volume works again or by the next pod.
2. **The supervisor restarts the pod that reports it.**
   - It acts only on a report from the Lease's current holder, never a predecessor's.
   - The cell's 2-minute cooldown applies.
   - When more than half the cells report at once, it alerts instead: that is a storage
     problem, and restarting every controller would not fix it.
3. **Longhorn leaves StatefulSets alone.** Its auto-delete blacklist gets `apps/StatefulSet`
   (`lab/longhorn-tune.sh`, `deploy/helm/README.md`).

## Consequences

- **Lab:** with JENKINS_HOME made unwritable under a running controller, the failure was
  reported 12 s later. The supervisor restarted the pod 1 s after that. The new pod probed
  cleanly and cleared the report.
- **Any storage.** The probe sees the failure from inside the pod, so it works on Longhorn,
  Ceph and NFS alike. A hung NFS mount is a failure too.
- **The setting is cluster-wide.** Longhorn's blacklist applies to every StatefulSet in the
  cluster, including those netCI does not run, which lose Longhorn's remount after an engine
  failure.
- **Slow disks.** A disk where three writes in a row take more than 10 s each restarts the
  controller once, and then raises alerts within the cooldown. Such a disk cannot hold a
  controller's `JENKINS_HOME` anyway (ADR-060's writeback bound).

## Rejected

- **Keep Longhorn's deletion and delay the replacement** until Longhorn has recorded its remount
  request. That would add seconds to every takeover, and it would depend on the timing inside a
  storage system that netCI does not control.
- **Restart the pod from inside**, by having the agent exit. A container restart keeps the dead
  mount; only a new pod mounts the volume again.
