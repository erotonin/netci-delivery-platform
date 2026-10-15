# ADR-089: A drain lets what is in flight finish; the balancers reload their configuration in place

- Status: accepted; measured in the lab (below)
- Date: 2026-10-15
- Refines: ADR-080 (drained rolling restart), ADR-085 (the chart)

## Context

The rolling upgrade to netci33 failed one UI read of 360. The lab holds 22,677 jobs, and its
dashboard takes 17 s to render. HAProxy's log showed what happened:
1. The draining replica's health check answered 503 (ADR-080).
2. The balancer marked it down.
3. `on-marked-down shutdown-sessions` closed the 17 s request in flight on it (`DCVN`).

`shutdown-sessions` is there for a machine that lost power: without it, an agent's WebSocket stays
open for the tunnel's hour, and the build it runs is not adopted. A drain is not that case, but the
balancer could not tell them apart.

Fixing it showed two defects in how the balancers are run:
- **The DaemonSet did not carry its configuration's digest.** A changed `haproxy.cfg` sat unread
  in the ConfigMap until someone restarted the balancers by hand.
- **Restarting a balancer takes the VIP down.** kube-vip elects the VIP's machine among the
  Service's endpoints, and restarts that election whenever an endpoint goes away. In the upgrade
  to netci34, replacing the balancers with a surge (a new pod ready before the old one stops) still
  refused connections for about 2 s: "existing endpoint has been removed, restarting
  leaderElection … Deleting VIP".

## Decision

1. **The plugin answers HAProxy's agent check** (`DrainAgent`, `NETCI_DRAIN_AGENT_PORT`, 8098). It
   says `maint` while `/tmp/netci-draining` exists and `ready` otherwise. In maintenance the balancer
   sends nothing new to the replica, cookie-pinned requests included, and lets what is in flight
   finish. With the agent, `/netci-ready/` stays 200 while the replica drains. Only a replica that
   is gone, or still starting, is marked down, and only then are its sessions closed.
2. **The preStop waits for what is in flight.** It:
   1. creates the drain file;
   2. gives the balancers 2 s (agent checks run every 1 s);
   3. polls `/netci-ready/busy` until no request is in flight, for at most `drainSeconds`.

   The count leaves out the drain's own polls and WebSockets (an agent's lasts hours). The chart
   refuses a grace period shorter than `drainSeconds` + 20.
3. **The balancers reload a changed configuration in place.** HAProxy runs master-worker under a
   loop that:
   - checks a changed file (the kubelet updates the mount within a minute) with `haproxy -c`;
   - reloads it with SIGUSR2, the old worker finishing its connections;
   - keeps a configuration HAProxy refuses out, and says so.

   The pod, its endpoint and the VIP stay. The image's stop signal (SIGUSR1, a soft stop) is passed
   on. A new balancer image still replaces the pods. For that the DaemonSet now surges (one new pod
   beside the old one on each machine) and the old pod leaves the endpoints 3 s before it stops
   listening.

## Measured in the lab

`phase6_upgrade_probe.py`: UI reads twice a second, two writers pinned by cookie, two people signed
in through the form.

| Run | Reads | Creates | Evidence |
|---|---|---|---|
| upgrade to netci33 (before) | 359 of 360: one cut in flight | 676, none missing | `…20261015T015907Z` |
| upgrade to netci34, balancers replaced (the digest) | 368 of 369: refused while the VIP moved | 523, none missing | `…20261015T023008Z` |
| the balancers' new command applied (replaced once more) | 223 of 223 | 374, none missing | `…20261015T023647Z` |
| a change of `haproxy.cfg` only: all 3 balancers reloaded in ~36 s, no pod replaced | 242 of 242 | 357, none missing | `…20261015T024008Z` |
| rolling restart, both replicas netci34 (the agent check in use) | 324 of 324 | 630, none missing; 308 form creates, none refused | `…20261015T024350Z` |

In the last run HAProxy logged `Server jenkins/r1 is going DOWN for maintenance … 0 sessions
active`, then `leaving forced maintenance` once the new pod's agent answered. Replacing the
balancers once more (third row) failed nothing, but replacing a balancer remains exposed to the VIP
moving; only the in-place reload avoids it.

The two scripts that send a password were still refused once each per roll (403, ADR-086's known
limit). The upgrade probe's r1 writer died in the netci34 run on a crumb it could not fetch (fixed:
the failure is now that request's answer), so that run has 20 of its writes.

## What it costs, said plainly

- **A stop waits for the slowest request in flight**, up to `drainSeconds` (30 by default). A rolling
  restart is that much longer per replica when someone is rendering a very large page.
- **The agent port is unauthenticated.** It answers one word and accepts nothing.
- **A new HAProxy image still replaces the balancers**, and with kube-vip's per-endpoint election
  the VIP can move once per machine. With an Octavia LoadBalancer the endpoints are health-checked
  by the load balancer instead.
- **A dashboard of 22,000 jobs takes 17 s**, and a person's first page after a replica restarts
  (cold) took 100 s in one run. That is Jenkins with that many top-level items, replicated or not;
  folders and views keep a page small.
