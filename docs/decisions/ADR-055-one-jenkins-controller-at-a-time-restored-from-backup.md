# ADR-055: One Jenkins controller at a time; the standby is restored, not running

Status: Proposed (research in docs/research/jenkins-ha-dr.md; lab in infra/corp/).

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
   S3-compatible object storage (MinIO in the lab). Velero, not Kopia alone: it backs up
   the namespace's Kubernetes objects and the volume together and restores them as one,
   and it uses Kopia underneath for the volume data (incremental, deduplicated,
   encrypted). A schedule runs every 15 minutes (RPO ≤ 15 min); a backup is also taken
   before any planned switch.
4. **Failover restores, then starts.** The standby is not a running second controller: on
   failure the active StatefulSet is scaled to 0 (or its node is gone), the latest Velero
   backup of `JENKINS_HOME` is restored into a fresh volume on a healthy node, and the
   controller starts from the same image and JCasC. `scripts/corp/jenkins_failover.sh`
   does this and measures RTO; a drill runs it on a schedule.
5. **Builds in flight at failure are lost, and netCI says so.** Their pods die with the
   controller; netCI's reconciler finds runs Jenkins no longer knows and fails them after
   the timeout (it does not report them as succeeded). They can be retried.

## Consequences

- netCI's multi-controller routing (ADR-009) stays for organisations that do run several
  controllers, but the company-like lab uses one.
- RPO is the backup interval; history newer than the last backup is lost on failover.
- Not verified until the failover drill has run in infra/corp.
