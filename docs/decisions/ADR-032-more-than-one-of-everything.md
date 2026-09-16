# ADR-032: More than one of everything -- API replicas, workers, and what had to move to the database

Status: Accepted.

## Context

netCI ran as one API process and one Temporal worker. Three things made a second API
replica wrong rather than merely useless:

- **Edge agents** registered in a process-local dict. A replica that did not hold the
  socket answered `AGENT_NOT_CONNECTED` for an agent that was connected, and
  `/api/v1/agents/status` listed only its own -- padded with invented `ip`, `os`,
  `version` and default CPU numbers for agents that had reported nothing.
- **Background loops** (reconciler, notification outbox) ran in every process. Two
  reconcilers would each "correct" the same stale run; two outbox workers would deliver
  the same webhook twice.
- **A worker that died mid-deploy** was noticed by Temporal only at the activity's
  `start_to_close_timeout` (10 minutes): the deployment sat `deploying` although a second
  worker was idle.

## Decision

**Agent connections and commands are rows** (`agent_connections`, `agent_commands`,
migration 0023; `backend/app/agent_fleet.py`). A replica upserts the connection when the
socket opens (with its `replica_id`), refreshes `last_seen_at` on every heartbeat, and
deletes the row only if it is still the holder -- a late disconnect on replica A cannot
erase the agent's fresh reconnection to B. An `execute` on any replica inserts a
`pending` command; the holding replica claims it (`UPDATE ... FOR UPDATE SKIP LOCKED`,
exactly once), sends it down its socket, writes the answer; the requesting replica polls
the row until it is answered or `expires_at`. The response names both replicas
(`requestedOn`, `claimedBy`). Status lists every agent every replica holds, flags `stale`
those whose replica stopped refreshing, and reports nothing the agent did not send.

**Singleton loops take an advisory lock per pass** (`pg_try_advisory_xact_lock`, held on
a guard connection for the duration of the pass). The replica that does not get it skips
the pass. In-memory mode always grants the lock: one process has nobody to race.

**Runtime activities heartbeat.** The playbook runner reports progress to Temporal every
10 s while `ansible-playbook` runs, and `deploy`/`rollback`/`health_check` carry
`heartbeat_timeout=45s`. A worker killed mid-playbook is replaced within that window; the
playbooks are idempotent, so the retry converges rather than doubles.

**Replicas are named** (`NETCI_REPLICA_ID`, default `<host>:<pid>`) so that a row, a log
line or a response says which process acted.

## What was rejected

- *Sticky routing of agents to replicas at the load balancer.* Requires the balancer to
  know netCI's hostnames; fails on replica loss anyway.
- *A message broker for commands.* One more component to run and secure; PostgreSQL
  already gives exactly-once claims with `SKIP LOCKED` and the audit trail for free.
- *Leader election for the loops.* A leader that dies leaves a gap until re-election; a
  per-pass lock has no leader to lose.
- *Sharing the websocket across processes.* Not possible; the socket is the one thing
  that stays local, and everything about it is now elsewhere.

## Consequences

- Two API replicas ran on the lab (`:8100` `api-a`, `:8101` `api-b`): an agent connected
  to A, `execute` on B answered in 270 ms with `claimedBy: api-a`.
- Two workers ran; `scripts/lab/worker_failover_drill.py` killed the one running a
  deployment's playbook (SIGKILL). Temporal started attempt 2 on the other worker 46 s
  later; the deployment was `healthy` 48.2 s after the kill (`evidence/worker-failover.json`).
- PostgreSQL tests cover the exclusive claim under two threads and the advisory lock
  refusing the second replica while the first is inside its pass.
- Not done: the *frontend* is served from one process; a load balancer in front of the
  replicas is the operator's; the lab has none (two ports).
