# Contributing to netCI

Thank you for improving netCI. Contributions are accepted under the Apache
License 2.0 and must preserve the platform's central rule: a green state is
reported only when real execution evidence proves it.

## Before opening a change

- Search existing issues and architecture decisions.
- Use an issue for changes to public interfaces, persistence, policy or the
  security model. Small documentation and test fixes can go directly to a PR.
- Never include credentials, production inventory, customer data or private
  artifact references. Follow `SECURITY.md` for vulnerabilities.
- Keep runtime integrations behind an existing seam, or explain why a new seam
  has at least two real adapters.

## Development setup

Read `QUICKSTART.md`, then run:

```bash
make frontend-install
make validate
make test
make release-portable
```

Changes to PostgreSQL require a forward-only migration plus an updated
`backend/schema.sql`. Changes to OpenAPI require contract tests. Changes to the
Portal require component tests; user-visible workflows require Playwright. A
runtime integration is not complete until its real acceptance gate produces
evidence on the required host.

## Pull requests

A reviewable PR:

- solves one coherent problem and links its issue or ADR;
- explains the risk, alternative considered and rollback path;
- includes tests at the module's interface;
- updates docs and examples in the same change;
- contains no generated secrets or historical evidence presented as current;
- passes `make release-portable` and any affected live gate;
- uses a Developer Certificate of Origin sign-off (`git commit -s`).

The DCO sign-off states that you have the right to submit the contribution
under this project's license. Maintainers may request a split when a PR mixes
unrelated refactors and behaviour changes.

## Commit style

Use concise imperative subjects, preferably Conventional Commits:

```text
feat(policy): bind waivers to immutable digests
fix(worker): reject inventory traversal
docs: explain deploy-time signature verification
```

## Review priorities

Reviewers prioritize correctness, security controls, durable state,
idempotency, operator recovery, accessibility and truthful observability over
surface-level convenience. Compatibility code must say when it can be removed.
