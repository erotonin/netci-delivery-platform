# ADR-056: netCI owns the toolchain versions builds run with

Status: Proposed.

## Context

Security evidence is only as good as the tools that produced it. Trivy with a
week-old vulnerability database finds nothing new; a cosign or syft upgrade on one
agent image changes what a signature or SBOM means. Today the versions are pinned in
`jenkins/agent-toolbox/Dockerfile` and nobody checks what a build actually ran.

## Decision

1. **One declared toolchain**: `toolchain/versions.yaml` names each tool (syft, trivy,
   cosign, buildah) with its version, download URL and sha256, the toolbox image built
   from it, and the Trivy DB mirror with its maximum age. The toolbox Dockerfile's
   versions are checked against it by a contract test, so the two cannot drift.
2. **Builds report what they ran**: the pipeline adds `toolVersions` (tool → version, and
   the Trivy DB's `UpdatedAt`) to the security evidence it already sends.
3. **netCI compares, and the policy decides**: evidence from an undeclared tool version
   or with a Trivy DB older than the declared maximum is refused (`TOOLCHAIN_DRIFT`,
   `TRIVY_DB_STALE`), fail-closed, with the declared and observed values in the reason.
   `NETCI_TOOLCHAIN_ENFORCE=warn` records the finding and allows, for a migration window.
4. **`GET /toolchain`** shows declared versions, what recent builds actually reported
   per controller, and the drift; the portal has a Toolchain page.

## Consequences

- Upgrading a tool is a change to `toolchain/versions.yaml` and the toolbox image, reviewed
  like code; a controller with an old toolbox is refused until it is rebuilt.
- Harbor's own scanner is not used for policy: netCI's decision rests on the build's scan.
- **Rollout order matters.** Enforcement is on by default (fail closed), and a library older
  than 0.4 reports no `toolVersions`, so a backend with this change refuses *every* build
  from such a controller (`ARTIFACT_POLICY_DENIED`, "the build reported no tool versions").
  Publish library 0.4 to every controller first, or run with `NETCI_TOOLCHAIN_ENFORCE=warn`
  until they are. The live stack still uses `netci-0.3`.
- **A report must name syft, trivy and cosign.** Those are the tools whose output is the
  evidence. A report that leaves one out is refused as `unreported`: the drift comparison
  skips absent tools, and without this check an omission would read as a match.
- Test fixtures that stand for a good build take their tool report from
  `backend/tests/toolchain_report.py`, which reads the declaration, so bumping a version does
  not turn unrelated tests red.
