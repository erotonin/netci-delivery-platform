# ADR-033: Jenkins is rebuilt or reloaded from git, never edited; secrets are files; a fresh machine is a script

Status: Accepted.

## Context

The controllers were already built from `jenkins/casc/*.yaml` (ADR-020/030), but three
operational gaps remained:

- Secrets (the Kubernetes service-account token, the pipeline key, the cosign private
  key) were passed as **environment variables** at `docker run`. Rotating the 24-hour
  token meant recreating the container (minutes of downtime per controller), and every
  secret was visible in `docker inspect`.
- A change pushed to `jenkins/casc` reached a running controller only when someone ran
  `jenkins_lab.sh publish` by hand; nothing compared the controllers afterwards.
- The lab had grown over days by hand. There was no single statement of what "a
  netCI machine" consists of, and no way to tell a fresh host what was missing.

## Decision

**Secrets are files under `/run/secrets`, read by JCasC.** JCasC resolves `${NAME}` from
a file of that name when no such environment variable exists. The controllers mount a
secrets directory read-only; the environment carries no secret. Rewriting a file and
POSTing `/configuration-as-code/reload` applies it -- no recreate.

**The Kubernetes token is bound to a revocable anchor.** `create token --bound-object-kind
Secret` ties the token to a Secret object; deleting the Secret makes the apiserver refuse
the token within its authenticator cache TTL (~10 s, measured). Rotation
(`jenkins_lab.sh rotate-token`, on a timer twice a day for 24 h tokens): mint into a new
anchor → write the file atomically → reload JCasC through netCI → delete the previous
anchor. A build ran on the rotated credential; the previous token was refused.

**Reload is a netCI endpoint, so it is authenticated, audited and followed by a drift
check.** `POST /api/v1/ci/controllers/reload` accepts the configuration repository's
push webhook (HMAC-SHA256 over the body, `X-Hub-Signature-256` or `X-NetCI-Signature`,
secret `NETCI_CASC_WEBHOOK_SECRET`) or a platform-admin token; anything else is 401/403,
and with no secret configured no signature is valid. It reloads every controller,
records `ci.controllers.reloaded`, then runs the controller comparison and returns it.
A controller that refused the reload is reported as such. Drift is also a metric
(`netci_ci_controllers_drift`, re-read at most once a minute per replica) with an
alert rule.

**A fresh machine is `scripts/bootstrap.sh`.** `--check` inventories every component
(tools, PostgreSQL, registry, Temporal, Keycloak realm and server, NetBox, kind with
namespaces/RBAC/registry mirror/kubeconfigs/ingress-nginx, Jenkins A/B and the git
server, the prod host, keys, ansible collections, the rendered profile, migrations, API
replicas, workers, monitoring, timers) and `--up` creates only what is absent. The
profile and the Keycloak realm are rendered from committed templates
(`infra/lab/real-local.env.template`, `infra/keycloak/netci-realm.template.json`) with
generated secrets; the templates contain none.

## What was rejected

- *Vault / SOPS in the lab.* The contract that matters is "secrets arrive as files a
  reload re-reads"; Vault Agent, a SOPS decrypt step or a Kubernetes Secret mount all
  produce exactly that directory. Choosing one for the lab would prove the tool, not
  the contract; the lab writes the files itself and says so.
- *Letting the git host call Jenkins' reload URL directly.* Jenkins' endpoint needs a
  Jenkins credential and a crumb, reloads one controller, and leaves no netCI record;
  the netCI endpoint reloads all of them and compares them.
- *Restarting the controller to rotate.* Minutes of unavailability twice a day for a
  credential that JCasC can re-read in a second.

## Consequences

- `evidence/casc-reload-and-rotation.json`: webhook reload 4.4 s for both controllers,
  drift false, 401 on a bad signature; rotation with the old token refused at t+10 s
  and a build succeeding on the new one; the same rotation run once through the
  systemd unit.
- `scripts/bootstrap.sh --check` reports "everything present" on the lab host. **It has
  not been run on a fresh machine**; `--up` is composed from the same ensure-steps the
  lab was built with, and that claim stays open until a clean host runs it.
- The first two revocation checks were wrong: `kubectl --token` with the lab kubeconfig
  still authenticated with that kubeconfig's client certificate, so the "revoked" token
  looked alive. The check uses `--kubeconfig /dev/null` now; the mistake is recorded in
  the evidence.
