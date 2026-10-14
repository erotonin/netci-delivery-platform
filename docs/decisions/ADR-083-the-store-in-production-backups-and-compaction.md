# ADR-083: The store in production: backups that are restored, a feed that is compacted

- Status: accepted; measured in the lab (below)
- Date: 2026-10-14
- Refines: ADR-076, ADR-080

## Context

The HA store (ADR-080) survives the loss of a machine, but two things would still have stopped it
in production:
- **No backups.** Replication copies a mistake to every instance at once: a job deleted by hand,
  or a bad migration. Nothing outside the cluster held the data.
- **No end to the feed.** `home_changes` keeps one row per change. A running build's copy is
  published every 3 s, so each build leaves dozens of rows, and a day of lab tests left 18,000.
  Nothing removed any.

## Decision

1. **Backups to S3-compatible storage**: Ceph RGW or MinIO at the site, the lab's SeaweedFS behind
   its CA.
   - WAL is archived continuously, and a base backup is taken daily (`ScheduledBackup`), kept 7 days.
   - CloudNativePG 1.30 still has the in-tree `barmanObjectStore`, deprecated and **removed in
     1.31.0**. The production chart uses the Barman Cloud plugin, which needs cert-manager; the
     settings carry over unchanged.
2. **A backup is only one when it has been restored.** `lab/aa/store.sh restore-test` recovers a
   new one-instance cluster from the object store and compares its rows with the store's. Run it
   after every change to backups, and on a schedule at the site.
3. **The feed is compacted, like a log.** Among changes older than a bound
   (`NETCI_HOMESYNC_COMPACT_AFTER`, 24 h), compaction deletes:
   - each change a later change of the same path has superseded;
   - everything of a running build's copy whose build has ended, its tombstone included.

   The latest change of every other path stays, so a replica reading from the start still learns
   every item, deletions included. Compaction takes the feed lock (ADR-080), so it decides on a
   prefix. Every homesync instance runs it hourly, taking turns on that lock.
4. **A replica behind the compaction reads everything again.** The feed's answer carries
   `compactedThrough`, the highest seq deleted.
   - A follower below it bootstraps again instead of going on: going on could apply a child before
     its parent, or never publish what only it has.
   - An edit catching up below it is refused until then.

## Measured / tested

- **Backup and restore** (lab, CloudNativePG 1.30.1, SeaweedFS):
  - continuous archiving and a base backup ran;
  - a cluster recovered from them in 384 s held 18,398 items and 126 build-number reservations,
    the same as the store.
  - On the way: the lab's key files end in a newline, which went into the S3 signature ("Invalid
    header value"), and the lab's S3 identity may write to one bucket only (AccessDenied). The keys
    are trimmed, and the backups use a prefix of that bucket.
- **Compaction** (Go, PostgreSQL): superseded old changes and an ended build's copy are deleted,
  recent ones kept, the feed afterwards is `a@3 b@1 c@1 c@2`, the mark is set, and a second pass
  deletes nothing.
- **Catch-up** (plugin): a home behind the compaction reads everything again. A job made while the
  store was down got published. With the re-bootstrap disabled it never was.

## What it costs, said plainly

- A replica down for longer than the compaction bound comes back by a full bootstrap, which it
  does on any start anyway.
- Backups need object storage that is outside the cluster's failure domain, and credentials for it
  in the cluster (a Secret). Restores are 6 minutes for this size in the lab.
- Finished builds' archives (`@builds/`) stay in the store until Jenkins deletes the build. Give
  every job a build discarder; the IDP's generated jobs carry one.
