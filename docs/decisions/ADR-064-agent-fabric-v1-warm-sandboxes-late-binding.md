# ADR-064: Agent fabric v1 -- warm sandboxes bound late to a controller

- Status: accepted
- Date: 2026-10-01
- Implements: ADR-061 decisions 1, 2 (`standard` class), 3 (limits, no API credentials), 6
  (sandbox level), 7 and 8. Host scaling (6, hosts), host recycling (4) and the `docker` and
  `untrusted` classes come later.

## Decision

**Pieces.**
- `netci-fabric` (Go) keeps a pool of warm sandbox pods per *pool* (image, resources,
  RuntimeClass, labels) in its own namespace and serves claims.
- The netCI plugin adds a Jenkins `Cloud`.
- `netci-sandbox` (Go) is the sandbox's entrypoint.

**Late binding.**
1. Jenkins needs an executor for a label one of its pools serves. The cloud provisions with no
   delay, and its launcher asks the fabric for a sandbox:
   `POST /v1/claims {cell, pool, agent, secret, controller}`.
2. The fabric takes a warm sandbox atomically, or creates one when none is warm, and keeps the
   binding (agent name, its inbound secret, the controller's URL) **in memory only**. The
   secret is a credential and is never written to the database.
3. `netci-sandbox` in the pod has been long-polling `GET /v1/binding`. It receives the binding
   and runs `agent.jar -webSocket` towards the controller. The build starts as soon as the agent
   connects; the image pull and the pod start happened before anyone was waiting.
4. After the build, the agent's retention terminates the node. The plugin calls
   `DELETE /v1/claims/{id}` and the fabric deletes the pod. A sandbox serves one build.

**A sandbox proves who it is with its own pod.** The pod gets a projected ServiceAccount token
with audience `netci-fabric` and a 10-minute lifetime, and no other API credential
(`automountServiceAccountToken: false`). The fabric checks it with a TokenReview and takes the
pod's name and UID from the review, never from the request. A pod can only receive its own
binding.

**The plugin authenticates to the fabric** with a per-cell bearer token, of which the fabric
stores only the SHA-256. The token decides the cell; a claim naming another cell is refused.

**State.** Sandboxes are rows in PostgreSQL:
`creating -> warm -> claimed -> bound -> released -> deleted`, with `failed`. Transitions are
compare-and-set, and each one is an audited event. The reconciler compares rows with pods
every second:
- a pod with no row is deleted;
- a row whose pod vanished becomes `failed`;
- a claim not bound within 2 minutes is released and its pod deleted;
- the pool is refilled up to its warm count, never above its maximum.

**The sandbox survives a takeover of its controller** (ADR-060). `netci-sandbox` restarts the
agent JVM when the agent exits or when the controller's `X-Jenkins-Session` changes (a
controller lost to power never sends a FIN). It stops only when the fabric says the claim was
released.

**Isolation (`standard`).**
- runc with a user namespace (`hostUsers: false`) where the node supports it;
- non-root, all capabilities dropped, no privilege escalation, RuntimeDefault seccomp;
- CPU, memory and ephemeral-storage limits;
- an `emptyDir` workspace;
- no API credentials.

**One active fabric replica.** Bindings live in memory, so the API and the reconciler run only
in the replica holding the fabric's leader lease. Its readiness reports leadership, so the
Service routes only to it. A failover loses unbound claims; Jenkins provisions again.

## Rejected

- **Putting the binding in a Secret mounted into the pod:** a Secret update takes up to a
  minute to reach a running pod, and it would put the agent's credential in etcd.
- **Exec into the pod to hand over the binding:** that needs `pods/exec`, which is far more
  power than a fabric should hold.
- **Using the Kubernetes plugin as it is:** the pod is created only after the build is queued,
  with no warm capacity and no binding late (ADR-061).
