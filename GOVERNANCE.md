# Governance

netCI currently uses a maintainer-led governance model.

## Roles

- Contributors submit issues, documentation, tests and code.
- Reviewers provide technical review but cannot merge their own approval.
- Maintainers manage releases, security response, repository settings and the
  final merge decision.

Roles are earned through sustained, constructive contributions. The current
maintainers are the people with merge permission in the GitHub repository;
repository permissions are the authoritative roster.

## Decisions

Routine changes use pull-request consensus. Public interface, domain invariant,
security boundary, persistence or deployment changes require an ADR under
`docs/decisions`. When consensus is not reached, a maintainer records the
decision and rationale in the ADR rather than resolving it privately.

No person may approve and merge their own security-sensitive change. Releases
must be based on a commit whose applicable release gates passed. Historical
evidence is never treated as proof for a newer commit.

## Releases

Releases use semantic versioning after `1.0.0`. Before that point, minor
versions may contain breaking changes, which must be called out in the
changelog and migration notes. Release artifacts should include provenance,
an SBOM, checksums and signatures when the public release pipeline is enabled.
