# ADR-037: The SCM is the organisation's, not netCI's -- why the lab has a 200-line git server and not GitLab

Status: Accepted (decision); GitLab deployment in the lab: blocked, reason recorded.

## Context

The lab serves the sample repositories from `scripts/lab/git_smart_http.py`: `git
http-backend` behind a CGI bridge, read-only, in the toolbox container. The question
was put plainly: why not GitLab, and if GitLab is better, deploy it.

## Decision

**netCI does not own source control.** It needs three things from an SCM and takes them
from whichever one the organisation runs:

1. a remote Jenkins can clone (`repositoryUrl`, any smart-HTTP or SSH git server);
2. `git ls-remote` for the run dialog's branches and tags (`GET /modules/{id}/git-refs`);
3. optionally, webhooks in and commit statuses out (`backend/app/adapters/scm.py`:
   `GitHubScmProvider`, `GitLabScmProvider`, `POST /webhooks/scm/{provider}`,
   `ScmCommitStatus`), so a push starts a run and the merge request shows netCI's verdict.

For 1 and 2 the lab needs only a git server, and the smallest correct one is the right
lab component: it speaks the same protocol GitLab does, so nothing measured against it
(checkout time, mirror refresh, ls-remote) is a property of the lab server. Replacing it
with GitLab would change the lab, not netCI.

**GitLab is better as the organisation's SCM, and netCI is written for it.** Webhook
token verification, event parsing, deduplication by delivery id and commit-status
posting for GitLab are implemented and unit-tested
(`backend/tests/test_scm_webhooks.py::test_gitlab_webhook_token_verification_and_trigger`).
In a real deployment `repositoryUrl` points at the GitLab project, the project's webhook
points at `/webhooks/scm/gitlab` with the shared token, and Jenkins clones with a
project access token from the secrets directory (ADR-033). None of that requires netCI
to host GitLab.

## Why GitLab CE is not deployed in this lab today

- Disk. GitLab CE is a ~3.3 GB image plus ~2 GB of data before the first project. The
  lab host is at **94 % (12 GB free)** after reclaiming everything that was netCI's to
  reclaim (retired Jenkins homes, an old docker-in-docker volume, unused images, build
  cache, the kind nodes' unused images). The remaining reclaimable space is OpenStack
  data the owner has chosen to keep. The DCIM pre-flight gate refuses production
  targets on a host above 90 % disk; installing GitLab would push the same host to
  ~97 % and stop every production deployment the lab exists to prove.
- Evidence. The integration is proven only by a webhook from a real GitLab reaching
  `/webhooks/scm/gitlab` and a status appearing on the commit. Until that has been run,
  the GitLab path is *implemented and unit-tested*, not *live-verified*, and
  `docs/LIVE-READINESS.md` §6 says so.

## What to do when a GitLab is available

1. Point the module at the project (`repositoryUrl`), register the webhook
   (`PUT /applications/{id}/scm`, provider `gitlab`, the token from the secrets dir).
2. Push a commit; confirm the run starts from the webhook, not from the browser.
3. Confirm the commit status on the GitLab project reads netCI's verdict.
4. Record all three in `docs/LIVE-READINESS.md` with the GitLab version and the run ids.

## Rejected

- *Gitea/Forgejo as a lightweight stand-in.* It would fit the disk, but it proves the
  Gitea path, which nobody asked for, and leaves the GitLab provider exactly as
  unproven as before.
- *Raising the DCIM disk threshold to make room.* The gate would then be lying about
  the host; the threshold is the fact, the free space is the problem.
