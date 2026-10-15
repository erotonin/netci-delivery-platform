# ADR-095: One build per commit; merge requests, forks, supersession and paths from a real GitLab

- Status: accepted; run live on the HA lab against the corp GitLab (below)
- Date: 2026-10-15
- Refines: ADR-043 (delivery rules), ADR-050 (admission and supersession), ADR-051 (path filters),
  ADR-054 (fork pull requests)

## Context

These features had been tested only with webhooks netCI's tests made themselves: merge requests,
forks, cancelling a running build, path filters. `lab/aa/idp_scm_probe.py` now drives the real
GitLab of the lab through its API, and the project's signed webhook carries every event:
- branches, commits and merge requests;
- a fork made by a second GitLab user, `outsider`, through an impersonation token, with no password.

Two defects came out of it.

1. **A branch's push and the merge request opened from it built their commit twice.** The two runs
   gave two digests for one source, and both were pushed under the commit's tag (MR !2). GitLab's
   own answer is workflow rules that drop the branch pipeline while a merge request is open.
2. **`**/*.md` did not match `README.md`.** `**/` was read as `.*/` and so required a directory. A
   README-only push to main deployed to dev, although the rule ignored Markdown.

## Decision

1. **A merge request from a branch of the same repository is in its branch's concurrency group**
   (`branch/<source>`). One from a fork keeps `pr/<n>`.
2. **An SCM event joins the build of its commit when one exists in its group**: queued, running or
   succeeded, with the same intent (build-only, publishing or not, no release tag) and the same
   configuration revision.
   - The event is logged on that build and audited as `pipeline.joined`.
   - Its delivery's idempotency key answers with that build.
   - The webhook answers `status: joined`.
   - A merge request's `cancelInProgress` still stops the branch's older commits' builds, whichever
     of the two events arrived first.
   - All of it happens under the group's advisory lock, in one transaction.
   - A failed or cancelled build is not joined: a new event builds again. A deploying push is never
     joined either; it builds what it deploys.
3. **`**/` is any number of directories, none included**, as in GitHub's filters.

## Tests, written with it

- **`test_one_build_per_commit.py`** (in memory, and a PostgreSQL race over five rounds between a
  push and its merge request sent by two API replicas):
  - joining in either order;
  - a finished build is joined, a failed one is rebuilt;
  - a fork, a deploy, another branch and another configuration are each built separately;
  - a retried delivery answers with the same build;
  - cancelInProgress through a join.

  With the join switched off, 6 of the 7 fail.
- **`test_path_filters.py`**: `**/*.md` matches `README.md` and `docs/a.md`, and not `app.py`.

## Measured on the lab

| Scenario | Result | Evidence |
|---|---|---|
| Push a branch, then open MR !6 | one build; the MR's event joined it; GitLab shows `netci/pipeline=success` linking the Jenkins build | `aa-idp-scm-mr-20261015T110818Z.json` |
| A fork's MR !4, `forkPullRequests: ignore` | nothing built | `aa-idp-scm-fork-20261015T110138Z.json` |
| The same MR, rules changed to `verify` through a config revision | one verify-only run, built from `refs/merge-requests/4/head`; Sign and Publish did not run; no tag in Harbor for the fork's commit | same |
| A new commit to an MR while its build ran | old build cancelled, `supersededBy` the new one, Jenkins `ABORTED`; the new commit built once and succeeded | `aa-idp-scm-cancel-20261015T110247Z.json` |
| Push rules with `pathsIgnore: ["**/*.md", "docs/**"]`; a README-only push to main | nothing built (before the fix it deployed); a code push deployed to dev | `aa-idp-scm-paths-20261015T110715Z.json` |

The module keeps the docs-ignoring push rules the last scenario set.

## Not done

- **A fast-forward merge to main builds the merged commit again** before deploying, instead of
  promoting the merge request's digest. A merge commit (GitLab's default) is a new commit anyway.
- **Not tested here:** GitHub, and a fork's merge request whose branch name collides with a branch of
  the target. The fork's own group, `pr/<n>`, keeps those apart.
