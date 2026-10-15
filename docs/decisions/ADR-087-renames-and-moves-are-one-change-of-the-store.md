# ADR-087: Renames and moves are one change of the store

- Status: accepted; unit-tested, measured in the lab (below)
- Date: 2026-10-15
- Refines: ADR-076 (replicated homes), ADR-078 (one owner per job)

## Context

A rename or a move ("Move" from cloudbees-folder) changes an item's full name. Jenkins moves the
item's directory, its builds with it, and then tells its listeners (`onLocationChanged`). It does not
save. The replicas' store knew items only by full name, so the other replica saw only what happened
next:
- the first save under the new name, which arrived as a new job with no builds;
- the old name, which stayed.

After a failover a person found two jobs, and the one with the new name had no history. A rename
also has to be ordered against builds. Jenkins refuses to rename a job while it builds, but it
checks only its own JVM. A build of the job on the other replica would have its directory moved
from under it.

## Decision

1. **The store moves an item in one transaction** (`POST /v1/moves`, `Store.Move`). The old name, its
   children and their finished builds (`@builds/<old>/…`) become tombstones. The same content appears
   under the new names. The jobs' leases and reserved numbers move too, so the owner goes on
   building, numbering from where it was. The feed carries:
   1. the old name's tombstone, with `movedTo` (new column `home_changes.moved_to`);
   2. the new names, parents before children;
   3. the builds;
   4. the other tombstones.
2. **Refused, with the reason, when:**
   - the item is not there, or the new name is (`NO_ITEM`, `EXISTS`);
   - the item or one of its children is locked by another replica (`LOCKED`);
   - a build under it is running, or about to (`BUSY`), on any replica.

   "Running" covers four cases:
   - its running copy is live;
   - its number was reserved less than 2 minutes ago and it has no finished record;
   - its copy was deleted less than 2 minutes ago and it has no finished record (the copy's deletion
     and the finished record are two listeners of one event, in no set order);
   - a running copy with no reservation at all.
3. **The plugin asks before Jenkins acts.** `EditOrder` recognises `confirmRename` and `move/move`. It:
   1. takes both names' locks;
   2. catches up;
   3. asks the store with a dry run.

   It refuses with the store's reason (409) before Jenkins moves the directory, because Jenkins cannot
   be made to take a move back. The move itself is published from `onLocationChanged`, under the same
   locks.
   - A folder's children also get `onLocationChanged`. They are recognised and not published: the
     store moved them with the folder.
   - Saves inside the request, under either name, are part of the move, not changes of their own. A
     save published first would make the new name exist, and the store would refuse the move onto it.
4. **A replica that holds the item renames its own copy** (`Job.renameTo` / `AbstractFolder.renameTo`,
   `Items.move`). Its builds go with the directory, and their archives are not fetched again. A
   feed entry it already holds is recorded without being applied:
   - a build that is there already;
   - a deleted build that is not there;
   - content equal to its own.

   If it cannot rename its copy, it falls back to the plain entries: delete, then fetch the new names.
5. **No build can overtake a move.**
   - `ReserveRun` refuses (`LOCKED`) while the job or one of its folders is locked by another
     replica, so a number is not reserved between the dry run and the move.
   - `ClaimJob` refuses a job whose store entry is deleted (`GONE`, 410). It waits on the item's row,
     which the move holds, so a replica that has not applied the move yet cannot take the old name and
     build there.
6. **A move refused after Jenkins made it** can only come from a script that bypassed `EditOrder`, or
   from a lost lock. The local copy is then moved back to the store's name. A move the store could
   not be reached for is deferred and retried by the lease thread, like any other publish.

## Tests

- **Go (PostgreSQL):** a folder moved with its job, two builds, lease and numbers (and `fx`, which
  only starts with the same letter, left alone). Refused for:
  - a build just given its number, a running copy, a copy just deleted;
  - onto an item, from nothing, into itself;
  - a child locked elsewhere, a stale token.

  A reservation waits while the folder is locked. A claim and a reservation racing the move, held
  in `beforeCommit`, each wait for it and are then refused. The HTTP codes are tested too.
- **Plugin (JenkinsRule, fake store):**
  - a rename here is one move: the new name is never published alone, both locks are let go, and
    the next build is #2 under the new name;
  - a rename is refused while the store says a build runs elsewhere, and Jenkins' copy keeps its name;
  - another replica's rename renames the job here, its build listed, no archive fetched;
  - a move into a folder, then out of it and the folder renamed by another replica;
  - a folder's rename is one move, its child not published again.
- **The plugin's 86 tests pass; SpotBugs reports 0 bugs.**

## Lab (controller netci32, homesync `cab7d33`)

`lab/aa/phase9_rename_probe.py`, evidence `aa-phase9-rename-20261015T014229Z.json`, each client pinned
to one replica through the VIP:

| Step | Result |
|---|---|
| T1: a job with 2 builds made on r0, renamed on r1 | on both replicas 1.1 s later with builds 1 and 2; old name gone on both; next build #3 |
| T2: renamed on r1 while it builds on r0 | 409: "#1 is running on jenkins-0; an item is not renamed or moved while it builds"; name unchanged on both; renamed at the first try once the build finished |
| T3: moved into a folder on r1 | on both 0.6 s later, builds 1-3 |
| T4: the folder renamed on r0, the job inside | on both 0.5 s later; build #4 started from r1 |

The rollout to netci32 itself was a Helm upgrade of the chart. The upgrade probe watching it died on
its own 120 s kubectl timeout (fixed in `phase0_probe.k`), so that run has no evidence. A
`--restart` roll measured afterwards is the evidence for netci32 (`aa-phase6-upgrade-20261015T014337Z.json`):
- the UI answered 356 of 356 reads;
- 624 creates were acknowledged, and none is missing on either replica;
- the two people signed in through the form made 270 creates, none refused;
- the two scripts sending a password were each refused once (403, ADR-086's known limit).

## What it costs, said plainly

- **A move rewrites every build of the item in the store.** Each archive is copied inside
  PostgreSQL, not over the network, but a job with thousands of builds makes one large transaction.
- **A build waits while its job or a folder above it is being edited on another replica.** An edit
  holds its lock for well under a second, so the build waits that long. A lock left by a dead replica
  holds it at most 30 s, until the lock expires.
- **Renames by script are checked after the fact.** A Groovy rename does not pass through
  `EditOrder`. If the store refuses it, the item is moved back.
- **netci-queue** dispatches by job name. A run queued for the old name is answered 404 by Jenkins
  after the rename. The IDP does not rename the jobs it creates.
- **Views were still not replicated** (done since, ADR-088).
