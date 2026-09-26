# ADR-055: One Jenkins controller at a time; the standby is restored, not running

Status: Accepted -- failover drill passed in the lab on 2026-09-26 (Evidence below). Research in
docs/research/jenkins-ha-dr.md; lab in infra/corp/.

## Context

The lab runs two Jenkins controllers side by side (A and B) and netCI routes builds
across them (ADR-009). A company runs one controller that people know, with its build
history in `JENKINS_HOME`. Two live controllers split that history, need identical
configuration kept in step (ADR-030 drift), and double the licence and resources. What is
wanted is continuity: when the controller fails, another one takes over with the same
configuration and the same history.

## Decision

1. **Exactly one controller is active.** It runs as a single-replica StatefulSet in the
   `jenkins` namespace with `JENKINS_HOME` on a PersistentVolume. netCI is configured with
   one controller whose URL is the Service, so a failover does not change netCI's config.
2. **Configuration is code (JCasC, ADR-006/033); only history is data.** Plugins come from
   the image, configuration and credentials from JCasC and secret files. A controller
   built from git is therefore correct without any backup; the backup carries build
   history, job state and `secrets/` that decrypt stored values.
3. **`JENKINS_HOME` is backed up by Velero with its Kopia file-system uploader** to
   S3-compatible object storage (SeaweedFS in the lab: MinIO no longer publishes community
   images). Velero, not Kopia alone: it backs up
   the namespace's Kubernetes objects and the volume together and restores them as one,
   and it uses Kopia underneath for the volume data (incremental, deduplicated,
   encrypted). A schedule runs every 15 minutes (RPO ≤ 15 min); a backup is also taken
   before any planned switch.
4. **Failover restores, then starts.** The standby is not a running second controller: on
   failure the controller's node is cordoned (or gone) and the `jenkins` namespace is
   deleted -- StatefulSet and PVC included, because Velero's file-system restore writes
   volume data only into a pod *it* recreates; a pod the StatefulSet recreated would start
   on an empty volume. The latest Velero backup is then restored, and the controller
   starts on the other node labelled `netci.io/jenkins-controller=eligible` from the same
   image and JCasC. `scripts/corp/jenkins_failover.sh` does this and measures RTO and RPO.
5. **Builds in flight at failure are lost, and netCI says so.** Their pods die with the
   controller; netCI's reconciler finds runs Jenkins no longer knows and fails them after
   the timeout (it does not report them as succeeded). They can be retried.
6. **`JENKINS_HOME` is a `local` PersistentVolume, not hostPath.** Velero's file-system
   backup skips hostPath volumes; kind's local-path provisioner makes one unless the PVC
   carries `volumeType: local` (infra/corp/storage-local-backup.yaml).
7. **The Kopia repository has its own random key.** Velero's default repository password
   is a published constant, and `JENKINS_HOME` holds both `credentials.xml` and the
   `secrets/master.key` that decrypts it. infra/corp/velero/install.sh sets the key before
   the first backup, because a repository keeps the key it was initialised with.

## Consequences

- netCI's multi-controller routing (ADR-009) stays for organisations that do run several
  controllers, but the company-like lab uses one.
- RPO is the backup interval; history newer than the last backup is lost on failover.
- The object store must have headroom: SeaweedFS at 512 MiB was OOM-killed while Kopia
  uploaded in parallel, which Velero reports only as a `Canceled` pod volume backup. It
  runs at 1 GiB with `GOMEMLIMIT`.
- Rotating the key means a new repository, not `kopia repository change-password`: that
  re-wraps the format blob and keeps the master key, which the public default password
  already exposed (docs/research/velero-backup-hardening.md). Velero has one repository key
  per install, so the switch is: rollback backup, new bucket and backup location, new key,
  a verification backup and a restore drill, then the old data destroyed.
- Not verified: TLS to the object store (the lab uses HTTP on the docker bridge) and an
  unplanned node loss (the drill cordons a live node).

## Evidence

2026-09-26, cluster `netci-corp` (3 control-plane + 3 workers), Velero v1.18.3 with Kopia,
Jenkins chart 5.9.64, `scripts/corp/jenkins_failover.sh`:

```
[10:15:07] marker: dr-marker build #2 is in JENKINS_HOME
[10:15:33] failure: controller node netci-corp-worker3 is cordoned (gone); deleting namespace jenkins
[10:15:49] restore: namespace jenkins from drill-20260926-101507
[10:16:17] restore restore-drill-20260926-101507-101549: Completed
[10:16:58] controller moved netci-corp-worker3 -> netci-corp-worker2
[10:16:58] RTO 84 s (failure to Jenkins answering), RPO 1 s (age of the backup restored)
[10:16:58] PASS: marker build #2 survived the failover
```

RPO is 1 s only because a drill backs up immediately before the failure; for an unplanned
loss it is up to the 15-minute schedule interval. The run before this one refused to
restore (`backup ... is PartiallyFailed, not Completed`) when the object store was
OOM-killed mid-backup -- the script restores only from a Completed backup.

2026-09-26 16:07, after the key rotation, restoring from the new repository (backup location
`jenkins-s3`, bucket `netci-jenkins-backups`, random key held outside the cluster):

```
[16:07:52] marker: dr-marker build #4 is in JENKINS_HOME
[16:08:33] restore: namespace jenkins from drill-20260926-160752
[16:09:01] restore restore-drill-20260926-160752-160833: Completed
[16:09:21] controller moved netci-corp-worker3 -> netci-corp-worker2
[16:09:21] RTO 63 s (failure to Jenkins answering), RPO 0 s (age of the backup restored)
[16:09:21] PASS: marker build #4 survived the failover
```

The run before it printed FAIL (`marker build #2 not found after restore (got '3')`) although
the restore worked: the script read `lastSuccessfulBuild` before the build it had just
triggered finished, so it marked an old build. It now waits for the build number it
triggered; the PASS above is from the corrected script.
