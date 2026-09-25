# ADR-051: Path filters skip only what is known to be unchanged

Status: Accepted.

## Context

A push that changes only documentation built, signed, scanned and deployed the whole
service. Agents make that common: many of their commits touch a README, a changelog or a
test fixture. GitHub Actions answers it with `paths` and `paths-ignore`. The danger is
the other direction: a filter that skips a build when netCI does not actually know what
changed would ship a change nobody built.

## Decision

1. **Triggers take `paths` or `pathsIgnore`** (not both, and not on tag rules: a release is
   built from what the tag names). A rule with `paths` matches only if a changed file
   matches one; a rule with `pathsIgnore` does not match when every changed file is ignored.
   A rule filtered out falls through to the next rule, like any rule that does not match.
2. **The changed files come from the SCM payload, and only when it is complete.** A GitHub
   push lists its commits' added, removed and modified files. It is treated as unknown when
   the branch is new, the push was forced, `before` is empty, the list is empty or malformed,
   or it is at GitHub's cap of 2048 commits. A GitLab push is complete only when
   `total_commits_count` equals the commits it lists. A pull request's payload carries no
   files, so it is always unknown.
3. **Unknown means the filter is not applied.** The rule matches, and the run's reason says
   "changed files unknown, path filter not applied", so nobody reads a full build as a
   filter decision.
4. **Stages are not filtered.** Skipping a required stage (SBOM, scan, signature) because of
   a diff would produce an artifact netCI refuses to deploy. Only whole runs are skipped.

## Consequences

- Pull requests always build in full until netCI reads their changed files from the SCM API.
  That call is not implemented.
- The glob is GitHub's branch-filter subset: `*` within a path segment, `**` across. No `?`,
  character classes or `!` negation.
