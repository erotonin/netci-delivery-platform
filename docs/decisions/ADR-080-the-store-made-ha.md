# ADR-080: The store made HA; a rolling upgrade measured; a deadlock the failover run found

- Status: accepted; measured in the lab (below)
- Date: 2026-10-14
- Refines: ADR-076, ADR-079

## Context

The active-active replicas (ADR-076..079) keep their homes the same through netci-homesync, and
netci-homesync was one PostgreSQL on a Longhorn volume. That was the last single machine whose
loss stopped edits. It had already happened:
- the lab's PostgreSQL was on the machine lost in ADR-079's run, and the store answered 503 for ~80 s;
- after the host restarted on 2026-10-14, every Longhorn volume came back faulted, and the store was
  down for ~10 minutes until Longhorn salvaged them.

The site mostly runs local-path, and so do the homes.

## Decision

1. **The store is three CloudNativePG instances, one per machine, each on its own machine's disk**
   (`local-path`). PostgreSQL replicates itself, as the homes are replicated through it. There is no
   shared volume, so no volume can come back faulted for all of them at once.
2. **Synchronous to ANY 1 of the two standbys, durability required.** A write is acknowledged only
   once a standby has it, so the standby promoted after the primary's death has every acknowledged
   write: a version compared-and-set, a build number reserved.
   - With one machine down, a standby remains and writes go on.
   - With two down, writes stop: the store refuses rather than forgets.
3. **The operator decides failover, so there are two of it, on two machines** (leader election,
   tolerations of 10 s for a dead node). One operator pod on the dead machine would have waited ~5
   minutes to be evicted before anyone promoted a standby.
4. **netci-homesync notices a dead peer in 10 s** (keepalives after 3 s, `TCP_USER_TIMEOUT` 10 s).
   pgx keeps the kernel's defaults, which retransmit for ~15 minutes. Under them, every pooled
   connection to the dead primary cost a request the plugin's 10 s timeout, and the LISTEN
   connection waited forever. It connects with `target_session_attrs=read-write`, so a connection
   never settles on an instance that is still a standby, or one again.
5. **Applying and publishing are ordered per item, not under one lock** (the plugin; see "Found").
   A publish can run inside Jenkins' locks. It waits for its item at most 10 s; after that it is
   deferred to the lease thread and redone from the home's content, read under the item's lock.

`lab/aa/store.sh install` sets this up:
- applies the operator release by checksum, with its image by digest;
- applies `lab/aa/store.yaml`, with PostgreSQL 16.15 copied into Harbor because the nodes could not
  finish pulling it from ghcr.io;
- copies the data with homesync stopped, checking the row counts on both sides;
- points homesync at the new store.

## Found: a deadlock, before anything was killed

The first failover run (`aa-phase6-store-20261014T023528Z.json`) went like this:
- r1 stopped taking changes 6 s *before* the power-off, and never recovered.
- r1's thread dump showed the cause:
  - A job created on r1 held Jenkins' item-group lock and, publishing from `onCreated`, waited for
    homesync's single lock.
  - The follower held that lock while applying a job r0 had created, and waited for the item-group
    lock.
- From then on, every edit on r1 queued behind the two.

Nothing about failover is needed: two replicas creating jobs at once can hit it. Phase 3's
concurrent edits changed existing jobs; they never created any.

Two tests now cover it:
- The lab's interleaving, forced through the fake store. It fails on the old code with "the create
  here never finished".
- A contended publish, deferred within its bound and done later. Writing it caught the deferred
  publish reading the home before taking the item's lock, which undid an applied change in the store.

## Measured (the lab, controller netci23, homesync 3e9469e)

**The store's primary machine powered off** (`lab/aa/phase6_store_probe.py`,
`aa-phase6-store-20261014T035606Z.json`). netci-lab-1 ran the primary, one homesync, one HAProxy and
jenkins-1. Each replica created a job every second; the UI was polled every second.

| | |
|---|---|
| Acknowledged creates | 271, every one present on both replicas afterwards |
| Build numbers reserved before | all on the promoted primary |
| Edits refused (503, fail closed) | from the power-off to 26 s; accepted again from 26-27 s |
| Failover | "Failing over" at 4.7 s, store-2 primary at 24.2 s, healthy at 71 s |
| UI | 148 of 149 polls answered (the one at the power-off failed) |
| The machine back | store-1 rejoined as a synchronous standby by itself |

**A rolling upgrade** (`lab/aa/phase6_upgrade_probe.py`, `aa-phase6-upgrade-20261014T035213Z.json`).
The controller image went from netci22 to netci23: jenkins-1 restarted, then jenkins-0, with
`minReadySeconds: 30` between them. It took 68 s, with the same writers and the UI polled twice a
second.

| | |
|---|---|
| UI | 286 of 286 polls answered |
| Creates | 270 accepted, every one present on both replicas |
| Refused | one POST per restarted replica: 403, a CSRF crumb from the replica that restarted (the probe fetches a new crumb and goes on) |

One more create timed out 15 s *before* the upgrade began: r1 was still in the deadlock above from
netci22, which the upgrade replaced.

**Nothing earlier regressed** with per-item locks and the HA store:
- Phase 3, all eight checks (`aa-phase3-20261014T040322Z.json`): a job on r0 reached r1 in 135 ms
  (median); 40 concurrent edits gave 30 accepted and 10 refused with the holder named, and both
  replicas converged; edits were refused with homesync down; the sign-in survived its replica, 60
  of 60 polls.
- Phase 5, pod killed (`aa-phase5-20261014T040446Z.json`): the build was adopted in 6.4 s, SUCCESS,
  every tick once.

## What it costs, said plainly

- **Edits are refused for ~25 s when the store's primary dies.** Most of that is CloudNativePG
  between noticing (4.7 s) and promoting (24.2 s). Reads, the UI and running builds go on: a build
  needs the store only to start (its number) and to be adopted.
- The 403 after a replica's restart is Jenkins' CSRF crumb, bound to the replica's session. A
  person's browser fetches a new one when the page reloads; a script must retry once.
- Three PostgreSQL instances cost ~150 MB of memory each in the lab, plus two operator pods.
- A publish deferred because its item was being applied is done within 5 s by the lease thread.
  Until then the other replicas lack that change; it is logged as such.

## Rejected

- **PostgreSQL on Longhorn with one instance**, as before: one volume, and it came back faulted.
- **Patroni with etcd**: a second consensus system to run beside Kubernetes'. The operator uses the
  API server's leases.
- **Asynchronous replication**: a failover could forget a reserved build number or an accepted
  version, and the replicas would hand the same number to two builds.
