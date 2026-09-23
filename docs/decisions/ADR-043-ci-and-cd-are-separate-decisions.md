# ADR-043: CI and CD are separate decisions

Status: Accepted.

## Context

A netCI pipeline run was one act: build, then deploy. Every successful build created a
deployment, so there was no way to build something without deploying it. The SCM webhook
made that worse:

- **Every event deployed.** A push to any branch and every pull request started a run in
  the application's default environment. A push to `feature/x` replaced what `dev` was
  serving, and so did a pull request nobody had reviewed.
- **A fork's pull request was signed.** Its build ran the Sign stage with the cosign key
  bound. That build runs the fork's code, which could read the key.
- **Webhook runs had no target.** They were started with `credentialsId` only, not the
  server-managed parameters the Run button resolves (`portal.delivery_parameters`). They
  were not pinned to the module's configuration revision either.
- **Monorepos built the wrong directory.** `NETCI_APP_DIR` existed only as a caller
  parameter, so a webhook run of a module in a subdirectory built the repository root.
- **Moving forward meant rebuilding.** Only production had "deploy this built digest",
  through a version. Reaching staging meant a new run, and so a new build of the commit.

GitHub Actions and OpenChoreo draw the line the same way
([Jenkins + OpenChoreo](https://www.jenkins.io/blog/2026/09/07/adopt-a-modern-idp-without-replacing-your-ci/)).
CI establishes one fact, that *this image, for this component, is ready*. Promotion is
an operation of the platform, not a stage of the pipeline, and it has its own access
control. We adopt that model. We do not integrate OpenChoreo itself: it is a second
control plane built for Kubernetes and Argo, and netCI deploys to hosts through Ansible.

## Decision

1. **Rules decide what an event causes.** They live in `pipelineConfig.delivery` of the
   module's configuration revision, so they are versioned, audited and approved like the
   rest of it. The first matching rule wins. `domain/delivery_rules.py` is pure:
   rules in, decision out. When a module declares no rules, it gets these defaults:
   - a push to `main` builds and deploys to the module's default environment, and only
     if that environment is dev or staging and has a target;
   - pushes to other branches and pull requests are built and not deployed;
   - a `v*` tag is built, and a semantic-version tag becomes a version.

   Patterns follow GitHub's semantics: `*` stops at `/`, `**` does not. Malformed rules
   are refused with a 422 rather than read in part.
2. **Production is never reached by a trigger or a promotion.** A rule with
   `deployTo: prod` is refused, and so is a promotion to prod (422
   `PRODUCTION_REQUIRES_REQUEST`). Production stays behind a production request and a
   second person (ADR-038/039). A pull request cannot deploy anywhere, because it is
   unreviewed code.
3. **Fork pull requests are ignored by default.** If a module opts in, they are
   *verify-only*: `NETCI_PUBLISH=false`, so the library skips Sign, Publish and Publish
   Evidence and the key is never bound. The run is recorded `publish_artifact = false`.
   The API refuses a digest for such a run (`UNPUBLISHED_RUN_HAS_NO_ARTIFACT`), because
   that report comes from the fork's own code. A CHECK constraint refuses it in the
   database too. A retry keeps the same intent. The fork's commit is fetched through its
   pull request ref, and netCI sends only the two shapes that exist:
   `refs/pull/N/head` and `refs/merge-requests/N/head`.
4. **Intent is fixed when the run is queued:** `deploy_after_build`, `publish_artifact`,
   `release_tag` and `trigger` (migration 0028). A build that is not deployed ends
   `succeeded`, with its digest and no deployment.
5. **A tag becomes a version in the same transaction that records the build's
   success,** through a hook the Portal sets on the delivery platform. If it were
   registered afterwards, a crash in between would leave a tag that no production
   request can name. A replayed callback could not repair that, because the run has
   already succeeded. When registration is refused (the tag was moved after a version was
   registered from it), the build stays succeeded and the refusal is audited.
6. **Promotion moves a digest and builds nothing.** `POST /modules/{id}/promotions`
   deploys a published run's artifact to dev or staging through `redeploy_artifact`,
   with the same lease, state machine and workflow as any deployment. A promotion rule
   may require that digest to have been healthy in an earlier environment for N minutes.
   Production requests honour the rule for prod.
7. **The soak is measured from `deployments.healthy_at`** (migration 0029). It is
   written once, by the transition to healthy, and stops counting when the next
   deployment in that environment became healthy. `updated_at` was not usable, because
   it moves on every later transition. Delivery events were not usable either: they are
   written for production only, because DORA reads them, and adding other environments
   would change DORA. Deployments that were rolled back or failed prove nothing.
   Deployments from before 0029 have no `healthy_at` and prove no soak.
8. **Changing the prod promotion rule needs a second person,** as changing a production
   target does. Removing a staging soak weakens the production gate.
9. **Webhook runs go through the same path as the Run button:** server-managed
   parameters, the active revision, and `pipelineConfig.buildInputs` (held to the
   `build_inputs` boundary). A run that deploys nowhere gets build inputs only, so a
   module without a target can still be built.

## Consequences

- **This changes behaviour.** A push to a branch other than `main` no longer deploys,
  and a pull request no longer deploys. A module that relied on the old behaviour
  declares a rule: `{"on": "push", "branches": ["**"], "deployTo": "dev"}`.
- An event no rule acts on is answered 200 `ignored`, with the reason. A 4xx would make
  the SCM mark the hook as failing and retry it.
- The Jenkins job gains two parameters, `NETCI_PUBLISH` and `NETCI_GIT_REF`. A
  verify-only build is also sent a `NETCI_STAGES` list with no `sign` and no
  `publish`. Every library since the third revision of `netciPipeline.groovy` honours
  that list, including the one on the lab's `main` branch, so a controller on an older
  library keeps the key out of a fork's build too. On such a controller the build then
  fails at Publish Evidence for want of a digest: a visible failure, not a leaked key.
  The first two library revisions did not read `NETCI_STAGES`; a controller still on
  one of those would bind the key. Deploy the current library before enabling
  `forkPullRequests: verify`.
- A retention job that removed old deployments would also remove their soak evidence.
  That errs toward refusing a promotion, not toward allowing one.
- **Not verified live.** The lab's git server is plain HTTP with no pull requests, and no
  run with these rules has been made against the lab's Jenkins. The tests drive the
  webhook, the CI callback and the promotion route, and PostgreSQL for the new columns
  and the constraint.
