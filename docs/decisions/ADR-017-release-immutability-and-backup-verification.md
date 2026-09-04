# ADR-017: Release Immutability, CI Report Separation, and Comprehensive Backup Verification

## Context

In Phase P0.4, two core reliability and data-integrity risks were addressed:

1. **Release Version Mutable Overwrites:** Previously, registering a release version could silently overwrite `artifact_digest` or release provenance using an `UPSERT` pattern. Furthermore, CI quality reports (test pass counts, coverage percentages, SAST/vulnerability scan outputs) were directly embedded into the mutable metadata of `release_versions`. This violated release immutability—an essential requirement for production compliance and reproducible deployments.
2. **Incomplete Backup Verification and Runtime Fake Data:** `scripts/netci_backup.py` previously monitored only 8 hardcoded tables, failing to verify critical application tables such as `systems`, `modules`, `release_versions`, `production_requests`, `security_evidence`, and newly introduced concurrency tables. Moreover, the frontend contained hardcoded fake notification entries ("Backend API v2.4.1...") and fake unread badges in `PortalShell.tsx`.

## Decisions

### 1. Immutable Release Versions
- `release_versions` rows are immutable once created.
- Attempting to register a version for a module with a different artifact digest or provenance now rejects the write with `VersionConflict` (HTTP 409).
- Idempotent re-registration is permitted if and only if the payload and artifact digest are identical.
- Audit records (`release_version.created`) explicitly record the actor, source pipeline run ID, module ID, version, and artifact digest.

### 2. Append-Only Version CI Reports
- Quality evidence and CI reports are decoupled from the release version row into a dedicated `version_ci_reports` table (migration `0011_release_immutability_and_ci_reports.sql`).
- Multiple reports can be recorded over time in an append-only fashion.
- The portal joins the latest CI report dynamically when querying release versions.

### 3. Dynamic Table Discovery and Comprehensive Backup Verification
- `scripts/netci_backup.py` dynamically discovers all base tables in `public` schema via `information_schema.tables`.
- Defines an explicit `CRITICAL_TABLES` tuple containing all 19 application tables.
- Verification enforces:
  - Presence of all critical tables.
  - Per-table row count matching.
  - Deterministic content checksum verification (`md5(string_agg(md5(t::text), '' ORDER BY t::text))`).
  - Referential integrity check across all foreign keys.
  - Verification into a clean throwaway database.
- Added `drill` subcommand to automate regular backup, clean verification, and failure injection testing.

### 4. Elimination of Fake Runtime Data
- Removed hardcoded fake notification and fake badge from `PortalShell.tsx`.
- Ensured `demo_data.seed_demo_data` strictly requires `NETCI_DEMO_DATA=true` and is off by default in production.
- Fixed TypeScript errors and missing imports in frontend, making production build (`npm run build`) and vitest 100% clean.

## Consequences

- Releases cannot be tampered with or silently mutated after issuance.
- Automated backup drills provide proof of recoverability and integrity.
- Zero fake notifications or mock artifacts appear in clean production environments.
