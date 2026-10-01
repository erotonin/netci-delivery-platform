# ADR-066: API clients keep working when an API server dies with its machine

- Status: accepted
- Date: 2026-10-01
- Refines: ADR-060 (unattended takeover)

## Context

A cell's machine can also run an API server: in the lab every machine is a control-plane
node, and small production clusters are often built the same way. When that machine loses
power, its API server's TCP connections neither answer nor close. Behind the `kubernetes`
Service, each client connection was balanced to one API server, so some of every client's
connections now lead nowhere.

Traces of unattended power-offs (`lab/spike/trace_takeover.sh`, probe evidence with the
supervisor's counters) found this in four places:

| Client | What it did | Cost |
|---|---|---|
| Cell Supervisor, pooled HTTP/1.1 connections | Two observations in a row waited out the 2 s timeout; the supervisor forgot what it had seen and started over | fenced at 9.7 s instead of ~4 s |
| Supervisor and cell agent before (HTTP/2) | One pinned connection carried every call | 7 s of failed calls; a healthy cell's renewals at risk |
| Longhorn CSI attacher (HTTP/2) | The leader lost its lease; no attacher left to move the volume | until a pod restarted |
| Longhorn manager (HTTP/2) | Its watches stayed on the dead connection for client-go's 30 s + 15 s; only it could stop a replica of the cell's volume | 44 s of the takeover |

## Decision

1. **netCI's own clients go through `internal/kubeclient`.** HTTP/1.1. Each attempt is bounded
   until its response headers arrive: 0.7 s for the supervisor, 0.5 s for its leader election,
   0.3 s for the cell agent and 2 s for the fabric. When an attempt fails, every connection of
   that client is closed, and a read is sent again on a new connection. The kubelet does the same
   after a failed heartbeat. Writes are not repeated by the transport, because only the caller
   knows whether its write may be repeated. Watches are refused rather than cut off.
   The supervisor goes further: it dials the API servers directly, from the endpoints of the
   `kubernetes` Service, and stays away for 30 s from one that failed an attempt. Bounding the
   attempts was not enough. Through the Service, each new connection still had a one-in-three
   chance of reaching the dead machine's API server until its endpoint was removed, 15-30 s
   later. Chaos series 7 then recorded the live leader failing three observations and starting
   over, so it fenced at 11.9 s. The request still names the Service, so TLS still verifies the
   Service's name, which every API server's certificate carries.
2. **Third-party clients on the takeover path get client-go's HTTP/2 health check at 2 s + 2 s**
   (`HTTP2_READ_IDLE_TIMEOUT_SECONDS`, `HTTP2_PING_TIMEOUT_SECONDS`): Longhorn's managers and
   CSI attachers. Attachers run one per node, spread per revision, and retry at most 5 s apart.
   These settings are applied as MutatingAdmissionPolicies (`deploy/longhorn/tuning-policy.yaml`),
   not as patches. Longhorn's driver deployer rewrites the attacher's Deployment every time it
   starts, so a patch was undone by the next takeover of its machine, and chaos series 10 ran
   mostly untuned. With the policies, the deployer was restarted and the attacher stayed tuned.
   The policies are shaped around a fault of the API server (k3s 1.36.4, cel-go 0.26.1). A JSON
   patch whose value is a list of typed objects panics the API server on every request the
   policy matches. The first version did exactly that to the attacher's Deployment and blocked
   updates to it until it was removed. Every variant has since been tried on a throwaway object
   first, while counting panics.
3. **A machine found off is fenced 3 s after its last renewal**, not at Lease expiry (ADR-060).
   That only helps if the observations around a power loss succeed, which decisions 1 and 2
   make so.

## Consequences

- In the lab, with all three decisions in place, the cell's machine was fenced 2.7 s after the
  power loss, and the build continued at 33.5 s. Before, those figures were 14.5-23 s and
  71-84 s. ADR-060 lists the runs.
- A cell machine that serves no API server does not meet this failure. Keeping the control
  plane on its own machines is still the better layout. These decisions keep a takeover fast
  when that layout is not possible.
- An API server that is slow to start answering the supervisor's lists makes it blind, not
  wrong: it acts on nothing it has not observed. `NETCI_API_ATTEMPT_TIMEOUT` raises the bound,
  and the `NetciSupervisorBlind` alert fires.
- The Longhorn settings are admission policies, so they survive the driver deployer's restarts,
  Longhorn's upgrades and a re-applied manifest. They are tied to Longhorn's object names, so a
  release that renames the attacher or the managers needs them updated. `lab/longhorn-tune.sh`
  fails if the attacher it finds is not tuned.

## Rejected

- **Waiting for the dead API server's endpoint to leave the Service**: that is up to 15 s plus
  a reconcile period, and connections that already exist are not affected by it.
- **Disabling keep-alive**: every request would need a new TLS handshake, on every client,
  forever, to save seconds on a rare event. Closing connections only on failure costs nothing
  when nothing fails.
- **Retrying writes in the transport**: a lease renewal or a fence step that is repeated after
  a reply was lost may act twice. Their callers already retry them with conditions such as
  resourceVersion, which keeps a repeat safe.
