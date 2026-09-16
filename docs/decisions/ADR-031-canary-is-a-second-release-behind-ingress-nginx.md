# ADR-031: A canary is a second release behind ingress-nginx, promoted by a deployment

Status: Accepted.

## Context

Until 2026-09-16 the "canary" strategy of a production request did three things: it
rolled the new digest onto the *only* release of the application (every request hit it
at once), wrote `traffic_weight = 10` on the deployment row, and told an in-memory
router about the 10 %. The router was a dict. `GET /deployments/{id}/traffic` then
reported `canaryWeight: 10` for an environment where 100 % of requests already ran the
new code -- and the in-memory router was the runtime default in production, not only
in tests. The fail-closed router (ADR-030 additions) turned that into a 501; this ADR
makes canary real.

## Decision

**The canary is a second Helm release.** `deploy-kubernetes.yml` takes a
`release_track`: `stable` (the ordinary release), `canary` (installs the digest as
`<release>-canary` beside stable, with the chart's Ingress rendered as an
ingress-nginx canary of the stable host at `canary_weight`), or `promote` (moves the
stable release to the digest, then removes the canary release). A rollback of a canary
removes the canary release; stable never changed. The track and the first weight are
deployment parameters the coordinator sets from the production request -- a caller
naming `release_track` or `canary_weight` is refused like any other server-owned key.

**Weight lives on the ingress, nowhere else.** `NETCI_TRAFFIC_ROUTER=nginx-ingress`
(`adapters/nginx_ingress_traffic.py`) finds the canary Ingress by labels
(`netci.io/application`, `netci.io/environment`, `netci.io/track=canary`) across
namespaces, patches `nginx.ingress.kubernetes.io/canary-weight` (and the header/cookie
rules), and reads the annotation back before reporting a weight. No canary ingress
means the weight is refused, not remembered.

**The first weight is confirmed when the canary is healthy, not when it is requested.**
At approval there is nothing to steer; the deployment row records the intended weight,
the playbook installs the ingress carrying it, and the coordinator asks the router to
confirm it when the worker reports `healthy`. `routerStatus` is therefore an
observation of the cluster; `trafficWeight` is netCI's intent.

**100 % is a promotion, not a permanent canary.** The last `advance` sets the canary
to 100 % and creates a `promote` deployment (approved by the reviewer who advanced),
so the environment ends with one release running the new digest. An abort sets 0 %
and rolls the canary deployment back, which removes the canary release.

## What was rejected

- *Keep the in-memory router as the default and document it.* A weight that routes
  nothing is exactly the false green the platform refuses elsewhere.
- *A service mesh.* The lab has none and the requirement is a canary, not a mesh;
  ingress-nginx's canary annotations are enough to move a percentage of requests and
  are observable with `curl`. The adapter interface leaves room for another router.
- *Blue/green through the same adapter.* Switching which Service the stable Ingress
  points at is a change to the stable release, not an annotation on a canary; the nginx
  router refuses `switch_route` with 501 rather than pretending.
- *Canary by replica ratio.* Two Deployments behind one Service split traffic by pod
  count, not by a number anyone set; the observed share would drift with autoscaling.

## Consequences

- The chart gained `netci.releaseName` / `netci.applicationId` (the stable host and
  the router's label), `canary.*`, a per-environment host
  (`<release>.<environment>.netci.local` -- the old default named `staging` in every
  environment), and a zero-trust policy that admits the ingress controller's namespace
  (kind's kindnet enforces NetworkPolicy; the previous policy blocked nginx with 504).
- The lab installs ingress-nginx from the lab registry (`scripts/lab/ingress_nginx.sh`,
  digests re-pinned to the mirror) on the worker node's ports 80/443.
- `scripts/canary_proof.py` drives a request through the steps and samples the
  ingress at each; `evidence/canary-nginx.json` records what share of answers carried
  the canary digest against the weight netCI reported.
- A canary needs the Kubernetes runtime and an ingress-nginx in front of it. The
  docker and systemd runtimes have no traffic router; a canary request for them
  reaches the worker as `release_track=canary` and their playbooks do not implement
  it -- they fail rather than deploy 100 % under a canary label.
