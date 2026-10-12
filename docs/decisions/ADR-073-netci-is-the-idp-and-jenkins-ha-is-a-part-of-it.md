# ADR-073: netCI is the IDP; Jenkins HA is a part of it, not its replacement

- Status: accepted
- Date: 2026-10-12
- Amends: ADR-060 (what netCI is), ADR-062 (decision 2, the removals)

## Context

ADR-060 and ADR-062 read the pain of a Jenkins controller lost with its machine as a reason to
change what netCI is, and the rebuild removed the Internal Developer Platform -- the portal, the
Python backend, Temporal CD, the catalog, the supply-chain checks -- keeping it only at the tag
`netci-0.3-cd-portal` (still `main`).

The owner of the project has said what it is: netCI is the IDP, the large problem. High
availability for Jenkins is one problem inside it, the one being taken furthest now. Nothing of
the IDP was to be deleted.

## Decision

1. **Both live in one tree, on one branch.** Every file the HA rebuild deleted is restored from
   `netci-0.3-cd-portal` unchanged (569 files: `backend/`, `frontend/`, `api/`, `templates/`,
   `sample-apps/`, the IDP's `scripts/`, `deploy/`, `jenkins/` and `infra/` files, `tests/`,
   `evidence/`); the IDP documents moved to `docs/archive/` go back to `docs/`. The HA layer stays
   where it is (`cmd/`, `internal/`, `jenkins/plugin/`, `deploy/helm/`, `lab/`).
2. **Where the two met in one file**:
   - `README.md` is the IDP's again, with a section on Jenkins HA; the HA overview is
     `docs/JENKINS-HA.md`.
   - `Makefile` is the IDP's (`make test`: pytest and the portal); the HA targets are in
     `mk/ha.mk` (`make check`: the Go gate). Only `help` and `test` collided; the Go `test` is
     `go-test`.
   - `.github/workflows/ci.yml` runs the IDP's four jobs and the HA's three, on every branch.
   - `.dockerignore` is the IDP's again (its images build from the root); the netCI Go image's
     allow-list moved to `Dockerfile.dockerignore`, BuildKit's per-Dockerfile ignore file.
   - `jenkins/Dockerfile.controller` carries both the IDP's shared-library snapshot, which the
     rebuild had dropped, and the HA's netCI plugin and unzipped plugins. The plugin set was the
     same on both sides.
3. **The IDP and the HA layer are to be joined**: the IDP's builds go through netci-queue (durable,
   idempotent dispatch, `netciOnce`) to cells that survive a machine's loss, instead of straight to
   one Jenkins. Not done yet.

## Verified

The IDP's gates on the merged tree, against a test database of its own (port 55434; 55432 is
`netci-p0-pg`, which is kept untouched): migrations and `schema.sql`, `pyflakes`,
`docker compose config`, pytest 62 + 1479 passed (1477 at first and one failure, the controller tag's
drift; then the fix and the new HA-lab test), the portal's 120 tests and build; `make check` and
`ci/charts.sh`. The restoring commit's message says 1478: one short, corrected here.

## Rejected

- **Keeping the IDP only on `main` and the HA on this branch**: two products that must be joined
  would drift further apart with every commit on either.
- **Restoring the IDP under a subdirectory**: every path in its code, scripts, CI and documents
  would change for no gain.
