# ADR-019: SCM Webhook Integration, Private Repository Checkout, and Commit Status Reporting

## Status
Accepted

## Context
Phase P1.1 (Phase 6) requires real SCM integration to replace manual and insecure repository triggers:
1. Webhooks from GitHub and GitLab must be verified with cryptographic signatures or token checks.
2. Webhook delivery IDs must be deduplicated atomically to prevent duplicate pipeline runs from network retries or replays.
3. Repositories must map to delivery applications server-side, preventing arbitrary tenant or repository spoofing.
4. Private Git checkout credentials must be referenced securely server-side (e.g. Jenkins `credentialsId`) rather than provided or exposed to the browser.
5. Commit status (`pending`, `running`, `success`, `failure`, `cancelled`) must be reported back to the SCM provider.
6. Jenkins console URLs must be persisted and exposed to the developer portal UI to enable the "Open Jenkins" action when a build is active or completed.

## Decision
1. **SCM Provider Port & Adapters (`backend/app/adapters/scm.py`)**:
   - Defined `ScmProvider` protocol with methods: `verify_signature()`, `parse_event()`, `report_commit_status()`.
   - Implemented `GitHubScmProvider`: validates `X-Hub-Signature-256` HMAC-SHA256 signature, parses push/pull_request events, reports status to GitHub Commit Status API.
   - Implemented `GitLabScmProvider`: validates `X-Gitlab-Token` using constant-time hash comparison, parses push/merge_request/tag events, reports status to GitLab Commit Status API.
   - Implemented `MockScmProvider`: in-memory provider with status recording for testing and local environments.
   - Enforced a 1MB payload limit (`MAX_WEBHOOK_PAYLOAD_BYTES`) to prevent denial-of-service via oversized payloads.

2. **Persistence & Atomic Deduplication (`backend/migrations/0012_scm_integrations_and_webhooks.sql`)**:
   - Created `scm_integrations` table linking `application_id`, `provider`, `repository_owner`, `repository_name`, `secret_token_hash`, and server-side `credential_reference`.
   - Created `scm_webhook_deliveries` table keyed by `(provider, delivery_id)`. Using database unique constraints ensures atomic deduplication across replicas.
   - Added `console_url TEXT` to `pipeline_runs` to store Jenkins/CI console output URLs.
   - Included both tables in `scripts/netci_backup.py` critical backup manifest.

3. **Secure Pipeline Triggering (`backend/app/main.py` & `backend/app/delivery.py`)**:
   - The `/webhooks/scm/{provider}` endpoint receives webhooks, checks payload length, verifies provider signatures, atomically inserts delivery records, looks up the corresponding application, and triggers CI using server-managed configuration (e.g. `credentialsId`).
   - SCM status notifications are emitted on pipeline start, running, success, and failure.
   - Secrets are never returned in GET responses (hashed or masked).

4. **Frontend Integration**:
   - `PipelineRun` in frontend client includes `consoleUrl`.
   - "Open Jenkins" button in `ModulePage.tsx` dynamically enables when `consoleUrl` is present.

## Consequences
- No client-supplied credentials or repository mappings are trusted from the browser.
- Webhook replays and duplicate deliveries are eliminated atomically.
- Downstream stages can track the build directly via the Jenkins console URL.
