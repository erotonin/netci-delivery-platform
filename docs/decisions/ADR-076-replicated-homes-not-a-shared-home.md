# ADR-076: Active-active on replicated homes, not on one shared home

- Status: accepted (design; phases 2-6 below build and test it -- none of it is established yet)
- Date: 2026-10-13
- Supersedes: ADR-074's storage decision ("one JENKINS_HOME on a shared file system").
  ADR-074's phase 0 and phase 1 results stand and are the reason for this one.

## Context

The mentor answered the three open questions (2026-10-13):
- **Jobs are Pipeline only.** No Freestyle; every build is a durable Pipeline that can resume.
- **Agents are inbound (JNLP).** They connect to the controller, not the controller to them.
- **Storage is mostly `local-path`; Longhorn exists.** A node-local disk is the normal case;
  a ReadWriteMany volume would have to come from Longhorn's share-manager.

What ADR-074 measured on a shared home (Longhorn RWX):
- **Phase 0:** nothing coordinates two Jenkins on one home (invisible builds, lost config updates).
- **Phase 1:** when the machine running the share-manager (the volume's NFS server) loses power,
  every write to the home blocks for **61 s even with fast failover** (134 s by default). That
  blocks *every* replica at once, because they share the one NFS server.

So with a shared home, one machine's death becomes a disk stall on all replicas, and the store
it needs is not the one the site mostly uses.

## Decision

**Each replica has its own `JENKINS_HOME` on its own node's disk (`local-path`). The homes are
kept the same by replicating Jenkins' objects through PostgreSQL, not by copying files.**

A machine's death then takes one replica and its disk; the other replicas' disks are untouched
and they keep answering. This is also the problem as first posed: two homes that are the same,
not necessarily one home.

ADR-074 rejected "two homes synchronised file by file" (rsync, lsyncd, DRBD), because copying
files does not order two writers' saves. This is not that. PostgreSQL orders every change:
- **Configuration** (jobs, folders, views): an item's `config.xml` is written to PostgreSQL first,
  under a per-item version (compare-and-set). The losing writer of a concurrent edit is refused
  and reloads; it is never silently overwritten (ADR-074 E4 was that silent overwrite). Other
  replicas reload the item on `LISTEN/NOTIFY`. Our own plugin does this (allowed by the mentor).
- **Global configuration and secrets** come from JCasC plus one `secrets/` set given to every
  replica. They must be byte-identical: an inbound agent's secret is an HMAC under them.
- **Build numbers** come from a PostgreSQL sequence per job, so two replicas never take one number.
- **Builds have one owner** (the replica netci-queue dispatched to). A finished build's record
  (`build.xml`, log, flow nodes) is published to PostgreSQL; the others materialise it, so every
  replica shows the full history.
- **A running build** is replicated as it runs (Pipeline writes `program.dat` and flow nodes at
  each step). When its owner stops renewing its lease, the owner is fenced first (supervisor and
  fence, ADR-060/069), then a survivor materialises the latest state and resumes the build.
  Pipeline only (the mentor's answer) is what makes this possible at all.
- **Inbound agents use WebSocket**: the same agent jar with `-webSocket`, through the load
  balancer. Checked in the running Jenkins 2.555.3 core: the agent sends `Node-Name` and
  `Secret-Key` on the `/wsagents/` upgrade, so HAProxy routes each agent to the replica that owns
  it. Over TCP JNLP (port 50000) the name is inside the protocol and the balancer cannot route it;
  that mode is to be measured as the alternative, not assumed.
  The `Secret-Key` header must never be logged, and must travel over TLS outside the lab.
- **The UI and API**: two HAProxy instances behind a VIP (kube-vip in the lab; on OpenStack an
  Octavia balancer or a VIP on allowed-address-pairs -- a question for the site). Sticky
  sessions; any replica answers lists and finished builds; a running build's pages go to its owner.

## What it costs, said plainly

- A finished build appears on the other replicas after it is published: a lag to measure, not zero.
- A running build on a dead replica pauses from the death until fencing and adoption complete.
  The UI stays up meanwhile; that build's own page says it is being adopted.
- Our plugin hooks Jenkins' save and build lifecycle. Jenkins upgrades must be tested against it,
  as with any plugin that does this (CloudBees' does too).

## Phase 2: the UI and API through an HA balancer (done)

`lab/aa/phase2.yaml`, `lab/aa/phase2_probe.py`: two replicas on `local-path`, an HAProxy per machine,
the VIP 192.168.122.240 held by kube-vip. Three probes every 0.5 s through the VIP, 2 s timeout:
`/login`, the API with basic auth, and a session signed in through the form. One power loss
(`virsh destroy`) per scenario, observed for 150 s. **One sample each**: these are what happened,
not a distribution.

| Run | Scenario | Failures (login / API / session) | Longest outage |
|---|---|---|---|
| v1: `externalTrafficPolicy: Cluster`, 2 HAProxy | a replica's machine | 5 / 5 / 4 | 4-6 s |
| v1 | the VIP's machine | 5 / 5 / 5 | 8-10 s |
| v2: `Local` | a replica's machine | 1 / 0 / 1 | 2 s |
| v2 | the VIP's machine | 7 / 7 / 7 | 14 s |
| v2 | the VIP's and the session's replica's machine | 15 / 16 / 15 | 30-32 s |
| **v3: `Local`, an HAProxy per machine, kube-vip on its own API server** | a replica's machine | 1 / 0 / 1 | **2 s** |
| v3 | the VIP's machine (with a replica on it) | 3 / 3 / 3 | **6 s** |
| v3 | the VIP's and the session's replica's machine | 1 / 2 / 1 | **2-4 s** |

What each change fixed, from the evidence:
- `Cluster` let kube-proxy on the VIP's machine send requests to a balancer on a machine that had
  just died, until its endpoint was removed. `Local` keeps traffic on the VIP's machine.
- kube-vip renewed its lease through the `kubernetes` Service, which still listed the dead
  machine's API server: each call landing there hung 10 s (`error retrieving resource lock`), so the
  VIP took 14-30 s to move instead of the lease's 3 s. Pointing kube-vip at its own machine's API
  server (every lab machine is a control-plane node) removed that.
- An HAProxy on every machine leaves every surviving machine able to take the VIP under `Local`.

What it does not fix yet, said plainly:
- **A signed-in session is lost when its replica dies**: the person is anonymous on the other replica
  and signs in again. Jenkins keeps sessions in one JVM. To be fixed by replicating users and the
  `secrets/` set (so a remember-me cookie or an SSO session is valid on every replica), or by SSO.
- The few seconds that remain are the 2 s client timeout of requests already sent to the dead
  machine, plus the 3 s VIP lease. Shorter timings are possible and were not tried.
- On OpenStack the VIP needs the port's allowed-address-pairs (or an Octavia balancer instead):
  a question for the site.

## Phase 3: the homes kept the same (done)

`lab/aa/phase3.yaml`, `lab/aa/phase3_probe.py`: netci-homesync (two, on the lab's PostgreSQL) and the
plugin on both replicas (controller `2.555.3-netci15`). Each client pinned to one replica through the
VIP. Evidence `aa-phase3-20261013T150720Z.json`.

| # | Experiment | Result |
|---|---|---|
| T1 | A job created on r0: until r1 has it (10 samples) | median **231 ms**, max 545 ms, none missing |
| T2 | ADR-074 E4 again: description on r0, then disabled on r1 | **No lost update**: both replicas have both changes |
| T3 | 40 description edits at once, half on each replica | 25 accepted, 15 refused with "being changed on replica ..." (409), none lost silently; both end on the same accepted edit |
| T4 | A job deleted on r1 | gone from r0 in 0.16 s |
| T5 | netci-homesync scaled to 0 | edits refused (503, with the reason), reads served (200); after it returned, an edit in 0.3 s and on r1 in 1.4 s |
| T6 | r1 down while r0 creates 3 jobs and edits one | r1 back in 20 s with every change |
| T7 | An API token minted on r0 (users replicated, ADR-077) | accepted on r1 after 0.05 s; revoked on r0, refused on r1 after 0.8 s |
| T8 | Signed in through the form on r0, r0's pod killed | 59 of 60 polls answered by r1, **all signed in as admin**, no failure |

The first run (`aa-phase3-20261013T101804Z.json`) found a real gap: a job created from XML
(POST createItem with a config.xml) is written and loaded without a save, so it was never published
-- ten jobs on r0, none on r1. Fixed (f52639b); the unit test now goes through that API. On the next
start, r0's bootstrap published those jobs and both replicas listed the same 18.

## Phases

| Phase | What | Pass |
|---|---|---|
| 2 | HAProxy + VIP before two replicas on `local-path`; the VIP's machine, a replica's machine, both, lose power | Done: 2-6 s, one sample per scenario (above); sessions are lost with their replica |
| 3 | Configuration (and users) through PostgreSQL: concurrent edits on two replicas | Done (above) |
| 4 | Build numbers and finished builds replicated | Every replica shows every build; the publish lag measured |
| 5 | Running builds replicated; adoption after fencing; WebSocket agents routed by name | A build on a dead replica finishes, every step once |
| 6 | Chaos and soak | Evidence in `lab/evidence/` |

## Rejected

- **One shared home on Longhorn RWX** (ADR-074's plan): phase 1's 61 s stall hits all replicas at
  once, and RWX is not what the site mostly runs. Kept as the measured alternative for a site with
  an HA NAS, where the same probe would decide.
- **Copying homes file by file**: see ADR-074.
- **A home per replica on Longhorn RWO, moved on failure**: that is the cell takeover of ADR-060;
  it keeps builds but not the UI, which is what active-active is for.
