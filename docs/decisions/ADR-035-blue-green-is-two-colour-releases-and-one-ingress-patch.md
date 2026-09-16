# ADR-035: Blue/green is two colour releases and one Ingress patch

Status: Accepted.

## Context

ADR-031 made canary real and left blue/green honest but absent: the nginx router
answered `switch_route` with 501, and a `blue_green` production request rolled the new
digest onto the only release while recording `activeColor: green`.

## Decision

**A colour is a release beside stable.** `deploy-kubernetes.yml` accepts
`release_track: blue|green` and installs `<release>-<colour>` with the chart's
`netci.track` set; a colour renders a Deployment and a Service and **no Ingress**. The
stable release keeps the Ingress for the host.

**The switch is one patch.** `NginxIngressTrafficRouter.switch_route(colour)` finds the
stable Ingress (`netci.io/track=stable`) and the colour's Service (`netci.io/track=<colour>`,
same namespace), requires the Service to have ready endpoints, patches the Ingress
backend service name, and reads back which colour the Ingress now names. `activeColor`
in `routerStatus` is that observation: `blue`, `green`, `stable` (the stable release's
own Service), or `unknown`.

**The colour is chosen at dispatch and switched when healthy.** The coordinator sends
the release to the colour that is *not* serving (green after a rolling release or when
blue serves) as a server-owned deployment parameter, and switches the Ingress only when
the worker reports the colour healthy. Nothing is switched at approval.

**Going back is `POST /deployments/{id}/traffic/switch`.** Both colours keep running,
so a switch-back (or forward) is the same patch, reviewer-authorised, recorded on the
deployment and in the audit log. The router refuses a colour that has no release or no
ready endpoints; a deployment that is not blue/green has no colours.

## What was rejected

- *Changing the Service selector.* One Service with a `version` selector means the
  idle colour has no Service of its own to health-check or to switch back to.
- *Deleting the previous colour after the switch.* Instant switch-back is the point of
  blue/green; retiring a colour is a separate, deliberate operation.
- *Switching at approval.* The colour does not exist yet; a switch to nothing is an
  outage reported as a release.

## Consequences

- Proven on the lab (`scripts/bluegreen_proof.py`, `evidence/bluegreen-nginx-{1,2}.json`):
  first blue/green after a rolling stable → green installed, ingress unchanged (0 %),
  switched on healthy (200/200 new digest), switch-back refused because no blue release
  exists; second blue/green → blue installed, switched (200/200), switched back to green
  (200/200 old digest), forward again (200/200).
- The stable release's own Deployment idles once a colour serves; two colours mean two
  running copies. A later "retire colour" operation is not written.
- A rollback of a blue/green *deployment* still goes through the playbook (the previous
  digest into the colour); the fast path is the switch endpoint.
- The Service track label exists only on releases upgraded with this chart; older stable
  releases report `activeColor: unknown` until their next deploy.
