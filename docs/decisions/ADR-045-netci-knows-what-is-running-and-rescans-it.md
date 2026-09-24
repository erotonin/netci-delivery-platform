# ADR-045: netCI keeps each artifact's SBOM and rescans what is running

Status: Accepted.

## Context

The first question a security team asks when a CVE is published is: *where are we
running it?* netCI knows which digest serves which environment of which module, which is
most of the answer. It could not give the rest:

- The SBOM was recorded only as a **path on the build agent**
  (`evidence.sbom.location`), and the agent is destroyed after every build. The
  document existed only as a Jenkins build artifact, for as long as Jenkins kept the build.
- The only vulnerability data netCI kept was the build's own scan: counts and the
  HIGH/CRITICAL identifiers, taken on the day of the build. A CVE published later was,
  by construction, absent.

## Decision

1. **CI uploads the CycloneDX document** after its evidence
   (`POST /pipeline-runs/{id}/sbom`). netCI records it against the digest *named in that
   run's evidence*, never against a digest the caller supplies, so a build cannot attach
   an SBOM to another artifact. The first document for a digest is kept, because a
   digest names immutable content.
2. **netCI rescans stored SBOMs itself** (`trivy sbom`, `NETCI_SBOM_RESCAN=trivy`), on a
   schedule (`NETCI_SBOM_RESCAN_INTERVAL_SECONDS`, default 6 h, one replica per pass under
   an advisory lock) and on demand (`POST /vulnerabilities/rescan`). Only digests that are
   serving somewhere are rescanned. Each scan replaces that digest's `rescan` findings as
   one set, in one transaction. A finding that is still present keeps `first_seen_at`.
3. **A failed scan is recorded as failed and changes no findings.** trivy missing, its
   database unreachable, a timeout, output that is not JSON: each raises. None of them
   ever produces an empty list, because "we could not look" must never read as "clean".
4. **The exposure API always states its coverage**
   (`GET /vulnerabilities/{id}/exposure`, `GET /vulnerabilities/exposure?minSeverity=`).
   Next to the affected modules and environments, it lists every running digest it could
   not vouch for: no SBOM recorded, never rescanned, or last rescan failed.
5. **The build's own findings are kept too** (`source = ci`), so a digest that was never
   rescanned still answers for what its build found.

## Consequences

- An answer is only as current as the last rescan and the trivy database it used. The
  coverage block says when that was. Before any rescan, `rescanned` is 0.
- Artifacts built before this change have no SBOM in netCI. They appear as not covered
  until they are rebuilt.
- The API image carries trivy, pinned to the version the build agents use and checked
  against its published SHA-256. Air-gapped installs point `NETCI_TRIVY_DB_REPOSITORY` at
  a mirror, as the build farm already does.
- SBOM documents are stored as JSONB. A few hundred KB per digest is the common case,
  and uploads above 16 MiB are refused.
