# ADR-028: What the first real end-to-end deployment taught

Status: Accepted.

## Context

On 2026-09-15 netCI ran, for the first time in this environment, against real
infrastructure end to end: Keycloak OIDC, two Jenkins controllers building on ephemeral
kind pods, a private registry, NetBox as DCIM, Temporal with a real worker, and Ansible
against this host. Every component had a green unit suite. The deployment still failed
four times before it succeeded, and each failure was a defect the suite could not see
because the fake on the other side of the seam shared the code's misunderstanding.

1. **cosign 3 signs in a format cosign 2 cannot read.** The CI toolbox pins cosign 3.1.2,
   which stores image signatures as sigstore bundles under the OCI referrers scheme. The
   worker host had cosign 2.4.1, which looks for a `<digest>.sig` tag, finds nothing and
   reports `no signatures found` -- the message for a *missing* signature, for an artifact
   that was correctly signed. The signature gate refused every artifact this platform
   produces. Readiness said `cosign: ready` because it only checked that *a* binary
   existed on PATH -- not the one the verifier was configured to run, and not its version.
2. **`delivery workflow failed: ActivityError`** was the whole failure reason recorded on
   the deployment. Temporal wraps the activity's exception; the workflow reported the
   wrapper's class and nothing else. An operator had to open Temporal's history to learn
   what netCI already knew.
3. **The callback token was on the `ansible-playbook` command line.** `callback_token` and
   `fencing_token` ride in `delivery.parameters` for the workflow's own use; the runtime
   runner passed every remaining parameter through `--extra-vars`, which `ps` shows to
   every user on the worker host. A bearer credential that can report this deployment's
   result was visible to anyone who could list processes.
4. **The playbook's install root was not configurable.** `/opt/netci-docker-demo` with
   `become: true` was the only layout, because the parameters that set it (`app_root`,
   `host_port`, `network_mode`) are -- correctly -- refused as per-run build inputs, and
   there was no server-owned place to put them. The lab, which must not modify `/opt`,
   failed with `Permission denied`.
5. **Inventory names did not match DCIM names.** NetBox knew the host as
   `netci-local-docker-dev`; the Ansible inventory knew it as `netci-local`. netCI
   validated the target against DCIM and then passed the DCIM name to `--limit`, which
   matched nothing.
6. **A no-op resubmission of the production target demanded an approver.** The API reads
   back unset fields as `null`; a revision built from that read-back compared unequal to
   the stored config (where the key was absent), so `_touches_production` fired. An
   approval that is demanded for nothing teaches people the approval is noise.
7. **A configuration revision's `deploymentConfig` was untyped.** Module creation
   validated every target; a revision -- which *becomes* the module's active target set,
   immediately for non-production -- accepted any JSON.
8. **The runtime runner reported stderr, else stdout.** `ansible-playbook` writes the
   failing task to stdout and only warnings to stderr, so the recorded reason for a failed
   deployment was `Warning: No resource found to remove` -- a harmless compose message --
   while the real one (the registry pull) was discarded.
9. **The registry has one name inside the build cluster and another on the deploy
   host.** CI on the kind network pushed to `172.17.0.1:55000`, which the artifact
   reference records. The host's Docker daemon trusts only `127.0.0.0/8` as an insecure
   registry (and this lab cannot edit `daemon.json`), so pulling the recorded reference
   failed with a TLS handshake against a plaintext registry.

## Decision

- **The verifier's cosign is pinned to the toolbox's major version**, and readiness
  reports the executable it resolved (`NETCI_COSIGN_EXECUTABLE`, not PATH) and its
  version, so a mismatch is visible before it blocks a deployment.
- **A failed deployment names the activity and the worker-side reason**
  (`activity validate_artifact failed: SignatureVerificationError: ...`). Activity
  messages are already bounded and never carry a credential, so they are quoted as-is.
- **Control-plane parameters never reach a command line.** `callback_token` and
  `fencing_token` are stripped in `command_for` before `--extra-vars` is built. A test
  asserts the token is absent from the rendered command.
- **`runtimeSettings` is reviewed configuration.** A deployment target may carry
  `appRoot`, `hostPort`, `containerPort`, `networkMode`, `appPort`, `systemdScope` and
  `become`, each validated (absolute path without `..`, unprivileged port range, closed
  enums). The Portal maps them, field by field and by name, to the playbook variables.
  The same names remain refused as build inputs: a run cannot move the install root.
- **Ansible inventory host names are the DCIM device names.** One physical lab host is
  six logical targets, all `ansible_connection=local`. The gate scripts pass `--limit`
  for the same reason netCI does.
- **Production-change detection ignores null-vs-absent.** Both mean "unset" to every
  reader of the config.
- **A revision's targets are validated like module creation** (`DeploymentTargetRevision`):
  environment, inventory-safe server names, namespace pattern, typed runtime settings.
  Unknown keys are kept for the risk classifier and never reach a playbook.
- **A failed runtime command reports both streams**, stdout then stderr, bounded.
- **`imagePullHost` is a runtime setting: a locator, not an identity.** It replaces only
  the host part of the recorded image reference; the digest is unchanged, and the
  container runtime verifies it on pull. This is the ordinary shape of a registry that
  is `registry.svc:5000` inside a cluster and something else on the hosts it serves.
  cosign verification still runs against the reference CI recorded, on the worker, before
  the playbook is started.

## Consequences

- The `.env.example` and `/readyz` both say which cosign is in use; an operator upgrading
  the toolbox is told to upgrade the worker.
- `DeploymentTargetRevision` keeps `extra="allow"` so the risk classifier still sees keys
  such as `memory`; a future revision may close that once the classifier reads a typed
  field instead.
- None of the seven defects above was findable with the fake adapters, and all seven
  were found within an hour of running against the real ones. The standing rule from
  CLAUDE.md -- "never claim live without evidence of a run against real infrastructure"
  -- is not caution; it is the only test that caught these.

## Rejected

- Downgrading the toolbox to cosign 2 to match the host. It moves the platform backward
  to keep a stale binary, and the referrers format is where the ecosystem is going.
- Accepting `app_root`/`network_mode` as build inputs "for the lab". That is exactly the
  per-run override of a host path the build-input policy exists to refuse.
- Rewriting the artifact reference at CI time to whatever the deploy host prefers. CI
  records where it pushed; that record is evidence and is not edited for a consumer.
- Passing the callback token to Ansible as an environment variable instead. It is not
  needed by any playbook; a credential that is not needed is not passed.
