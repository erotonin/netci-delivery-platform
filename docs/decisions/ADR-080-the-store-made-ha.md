# ADR-080: The store made HA; a rolling upgrade measured; a deadlock the failover run found

- Status: accepted; measured in the lab (below), soak included
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
6. **The feed's order is its commit order.** Every change takes one advisory lock just before it
   is placed in the feed, and holds it until the commit (see "Found").
7. **The store's instances and the operator talk to their own machine's API server**, not the
   Service in front of all three (see "Found"). A machine's API server lives and dies with the pods
   on it.
8. **A running build's copy holds everything else first and its log last, flushed.** The log then
   holds all the output program.dat says was read. Watched output also carries the agents' marks,
   which the adopter puts back (see "Found").
9. **Agents notice a dead path in seconds.** Replicas ping every 5 s with a 10 s timeout, for both
   sides. The lab's agents use the balancer on their own machine and give up a connection attempt
   after 3 s; an agent outside the cluster uses the VIP.
10. **A replica leaves traffic before it stops.** The balancer checks the plugin's `/netci-ready/`.
    The pod's preStop makes it answer 503 and waits 6 s before Jenkins is told to stop. A 503 that
    still gets through is retried on the other replica.

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

## Found: an acknowledged create skipped by one replica

A later run (`aa-phase6-store-20261014T043727Z.json`) checked every create answered 200 on both
replicas. One, `st-043727-r1-2`, was missing on r0, although it was in the store at seq 3205, 9 s
before the power-off. Nothing was logged. The cause:
- `home_changes.seq` comes from a sequence, handed out at insert time.
- Transactions commit in any order, so the one holding 3206 committed first.
- r0's follower read 3206, moved past it, and never read 3205.

Synchronous replication made each commit long enough to be overtaken. The single PostgreSQL before
had the same hole, only narrower. The test holds one change before its commit while another is
made: without the lock the reader sees only the second, with it both. A replica that missed a
change before the fix gets it back when it restarts: its bootstrap compares every item with the
store. r0 had `st-043727-r1-2` after the restart.

## Found: a standby's machine lost restarted the healthy primary

The first soak (`aa-phase6-soak-20261014T040915Z.json`) powered off the machine of a jenkins
replica, which also ran a standby. Three minutes later edits were refused for ~4 s: the
**primary's** postgres had been restarted by its own liveness probe.
- Its instance manager reached the API server through the Service, and its pooled connection sat on
  the dead machine's API server for ~40 s.
- CloudNativePG's isolation check then reached neither the API server nor the dead standby, and
  failed the primary's liveness.
- The kubelet restarted a primary that had nothing wrong with it.

With each instance on its own machine's API server, a standby's machine lost cost no edit:
`aa-phase6-store-20261014T043239Z.json` shows r1 refused nothing, and r0 lost two writes only
because jenkins-0 was on that machine.

## Found: an adopted build missing two lines of its log

A rerun of the soak's machine loss alone (`aa-phase6-soak-20261014T054113Z.json`) adopted
`long #30` with SUCCESS, but ticks 10 and 11 were not in its log; the `sh` step had printed them.
The lab's durable steps poll:
- the controller reads the agent's output file from `lastLocation`, writes it to the build log,
  and saves `lastLocation` in program.dat;
- the copy took `log` before `program.dat` (name order), so a read between the two left
  program.dat past the copy's log, and the adopter read on from there;
- and a Pipeline log is buffered (seconds) before it reaches the file.

A copy now holds everything else first, then the log, flushed just before it is copied. The log
then holds all output up to program.dat's position. At worst, a read landing between the two files
is shown twice: duplicated, never lost.

Where output is watched (durable-task's `USE_WATCHING`), the agent keeps the mark itself. A copy
records each running `sh` step's mark together with the log's length read just after it. The
adopter puts the agent's mark back to the copy's, and moves it on only past what was appended to
the log after the mark, when that is exactly the output following the mark. "Whatever the log ends
with" would have skipped a repeated line the log never got.

The tests:
- the log is the archive's last entry;
- only output provably appended counts;
- a mark is put back, never forward;
- a watched `sh` step's copy holds every tick up to its mark. This one failed ("lacks tick 11")
  until the log was flushed.

## Found: an agent connected to a dead machine for 9 minutes

In 1 of 2, then 1 of 3 machine-loss rounds the build was adopted only after ~8.7 minutes, yet
still SUCCESS with every tick. The agent's log showed why: agent-0 stayed "Connected" to the dead
jenkins-0 until its own remoting ping failed (Jenkins defaults: every 300 s, 240 s timeout).
- The lab's agents are pods. kube-proxy sends their connection to the VIP to the HAProxy of any
  machine, and in those rounds it was the HAProxy on the machine that lost power.
- A machine without power sends no reset, so the connection went silent.
- An agent outside the cluster, as at the site, meets the same thing when the machine holding the
  VIP dies.

The replicas now set a remoting ping every 5 s with a 10 s timeout. The controller sets the
agent's pinger too, so an agent on a dead path reconnects within ~15 s.

## Found: a replica stopping answers 503 for ~2 s

A second rolling upgrade (`aa-phase6-upgrade-20261014T061407Z.json`) failed 2 of 280 UI polls, one
at each replica's restart. HAProxy retried connection failures and empty responses, but not a
503. A stopping Jenkins answers 503 until its health check fails twice. HAProxy now retries a 503
on the other replica. That is safe: Jenkins says 503 before it acts on a request, and so does the
plugin when it refuses an edit.

## Measured (the lab: controller netci25, homesync with every fix above)

Each run below is kept in `lab/evidence/`. The load was the same throughout:
- each replica created a job every second through the VIP;
- the UI was polled through the VIP (every second, or twice a second in the upgrades);
- every create answered 200 was checked on both replicas afterwards.

**The store's machines.** These are `phase6_store_probe.py` runs on the feed fix.

| Machine powered off | Acknowledged creates | Missing on either replica | Edits refused | UI |
|---|---|---|---|---|
| The primary's, with jenkins-0 on it (`aa-phase6-store-20261014T045629Z.json`) | 268 | none; reserved numbers all on the promoted standby | 503 until 27.6 s (promoted at 22.6 s) | 148 of 149 |
| A standby's, with jenkins-0 on it (`aa-phase6-store-20261014T043239Z.json`) | 317 | none | none on r1; r0 lost 2 only because jenkins-0 died | 148 of 149 |

**The soak** (`phase6_soak.py`, `aa-phase6-soak-20261014T073506Z.json` plus 075230Z and 080719Z).
Each round started `long` (90 one-second ticks on an inbound agent) and injected one fault 10 ticks
in. There were 11 rounds:
- a jenkins pod killed, its replica's machine powered off (4 rounds);
- the store's primary pod killed, its machine powered off (3 rounds);
- a homesync pod killed;
- the HAProxy pod holding the VIP killed.

| | |
|---|---|
| Builds | 11 of 11 SUCCESS; every tick exactly once; each listed on both replicas; numbers distinct |
| Build finished after the fault | 83-85 s every round, as if nothing had happened (90 ticks, 10 already done) |
| Acknowledged creates | 3235, every one on both replicas |
| UI | 1742 of 1750 polls answered (each failure at the instant of a death) |

**Rolling restarts and upgrades** (`phase6_upgrade_probe.py`). Each one went one replica at a time,
with `minReadySeconds: 30`.

| Run | UI polls answered | Creates | Not accepted |
|---|---|---|---|
| netci22 to netci23 (`aa-phase6-upgrade-20261014T035213Z.json`) | 286 of 286 | 270, all on both | one 403 per restarted replica |
| netci23 to netci24, before the 503 retry (`...061407Z`) | 278 of 280 | 279, all on both | one 403 per restarted replica |
| restart, 503 retried, no drain (`...081554Z`) | 295 of 297 | 298, all on both | one 403 per restarted replica |
| **restart with the drain** (`...083045Z`) | **306 of 306** | 303, all on both | one 403 per restarted replica |

The 403 is Jenkins' CSRF crumb. The probe signs in with a password and keeps a crumb bound to a
session on the replica that restarted; it fetches a new one and goes on. A script using an API
token needs no crumb and does not see it.

**Nothing earlier regressed**, checked on netci23 with the HA store:
- Phase 3, all eight checks (`aa-phase3-20261014T040322Z.json`): a job on r0 reached r1 in 135 ms
  (median); 40 concurrent edits gave 30 accepted and 10 refused with the holder named, and both
  replicas converged; edits were refused with homesync down; the sign-in survived its replica, 60
  of 60 polls.
- Phase 5, pod killed (`aa-phase5-20261014T040446Z.json`): adopted in 6.4 s, SUCCESS, every tick
  once.

Superseded, kept for what they found: `aa-phase6-store-20261014T023528Z.json` (the deadlock),
`...035606Z` (on the feed bug, before it was known), `...043727Z` (the skipped create),
`aa-phase6-soak-20261014T040915Z.json`, `...050449Z` and `...054113Z` (the liveness restart, the
9-minute agent, the two lost lines).

## What it costs, said plainly

- **Edits are refused for ~25 s when the store's primary dies.** Most of that is CloudNativePG
  between noticing and promoting (noticing takes 4-20 s, longer when the operator's leader died
  too). Reads, the UI and running builds go on: a build needs the store only to start (its number)
  and to be adopted. Losing a standby's machine costs no edit.
- A job created in the second before its machine lost power can be left empty on that machine's
  disk (Jenkins writes it without a sync): it failed to load there ("Premature end of file"). The
  replica's bootstrap wrote it again from the store, and both replicas had it.
- Feed writes are ordered through one lock: the store takes one change at a time from the feed
  insert to the commit (a synchronous standby's acknowledgement included). That is a few hundred
  changes a second, far above what Jenkins edits and finished builds produce.
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
