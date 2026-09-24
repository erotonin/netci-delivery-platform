# ADR-049: A preview environment is deployed, or it does not exist

Status: Accepted.

## Context

netCI had `preview_environments` and an API to create one. Creating a preview wrote a row
with `status = active` and a URL of the form `https://prv-<app>-<pr>.preview.netci.internal`.
Nothing was deployed and nothing served that URL. A reviewer following the link found
nothing, and every screen in between said it existed.

## Decision

1. **A preview belongs to the pull-request run that built it.** It is requested when a
   same-repository PR run succeeds with a published digest and the module enables
   previews (`pipelineConfig.previews`). It can also be requested explicitly for such a
   run. A fork's PR is verify-only (ADR-043), so it has no artifact and never gets a preview.
2. **The worker deploys it, and only its report makes it `active`.** A row is
   `deploying` until the worker reports back, with its own single-use scope
   (`preview:result`, bound to the PR run). A result without a URL leaves the URL empty:
   the URL is the host of the Ingress the cluster actually created, read back by the
   playbook, never composed by netCI.
3. **Previews run under a playbook of their own** (`preview-kubernetes.yml`), not the
   production one. That playbook accepts only a `preview-<module>-pr-<n>` namespace. It
   reads the namespace before creating it, because an apply would silently label an
   existing namespace and then pass its own check. It creates the namespace labelled
   `netci.io/preview=true`, and deletes a namespace only if it carries that label. The
   production playbook still refuses any namespace but dev, staging and prod.
4. **Previews end:** when the PR is closed or merged (from the SCM webhook), when their TTL
   (1 to 72 hours) expires (a reaper under an advisory lock), or on request. Teardown is
   itself a worker operation with a reported result.
5. **Rows from before this change become `unverified`** (migration 0032). They were never
   deployed, so calling them `active`, or even `expired`, would still be a claim.

## Consequences

- Previews need a Kubernetes runtime and a dev target with a kubeconfig. Docker and
  systemd modules cannot have previews, and configuring one for them is refused.
- The kubeconfig the worker uses must be allowed to create and delete namespaces. That
  is a wider permission than deploying into existing ones, and the label check in the
  playbook is what keeps it from reaching other namespaces. RBAC that limits it further
  (for example, a controller that only admits `preview-*` names) is recommended and not
  shipped.
- Not verified live until a PR preview has run on the lab cluster.
