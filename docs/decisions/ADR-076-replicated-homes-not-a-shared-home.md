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

## Phases

| Phase | What | Pass |
|---|---|---|
| 2 | HAProxy pair + VIP before two replicas on `local-path`; the VIP's machine, a replica's machine, both, lose power | UI and API probed every 0.5 s through the VIP: failures and longest outage measured, sessions after failover |
| 3 | Configuration through PostgreSQL: concurrent edits on two replicas | No lost update; both replicas converge; the loser is refused, not overwritten |
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
