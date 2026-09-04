# ADR-021: Versioned Environment Configuration and DCIM Lifecycle

## Status
Accepted

## Context
In a multi-tier delivery platform, pipeline and deployment settings (such as runner assignments, stage definitions, target hosts, replica counts, and DCIM service bindings) are central to the integrity of software releases. Previously:
1. Module configuration was stored as mutable columns directly on the `modules` table. Editing an environment or stage silently overwrote the previous settings, making historical auditing impossible.
2. Pipeline runs and deployments did not pin configuration at execution time: if an operator changed settings while a deployment was in flight, the deployment could execute with an inconsistent mix of old and new targets.
3. Production configuration lacked formal change control: any developer could reconfigure production target servers without approval or separation of duties.
4. Deployment dispatch lacked target host revalidation: if a server had been decommissioned, placed into maintenance, or taken offline in DCIM, deployments would attempt to execute against an unavailable or unsafe target.
5. Configuration drift between the desired state in netCI and the live running deployments or DCIM infrastructure was undetected.

## Decision

1. **Immutable Configuration Revisions (`module_config_revisions`)**:
   - Created `module_config_revisions` table via migration `0014_versioned_config_revisions_and_dcim.sql`:
     `id UUID PRIMARY KEY`, `module_id`, `revision_number`, `pipeline_config JSONB`, `deployment_config JSONB`, `change_summary TEXT`, `status VARCHAR`, `created_by`, `approved_by`, `approved_at`, `rejection_reason`, `created_at`.
   - Each revision is strictly immutable once written. Updating configuration always appends a new monotonic revision (`revision_number`).
   - Added to `scripts/netci_backup.py` critical backup manifest and integrity checks.

2. **Active Revision Pointer with Optimistic Locking (CAS)**:
   - Added `active_config_revision_id UUID` and `config_version INTEGER NOT NULL DEFAULT 1` to `modules`.
   - Activating a revision uses compare-and-set:
     `UPDATE modules SET active_config_revision_id = %s, config_version = config_version + 1 WHERE id = %s AND config_version = %s`.
   - Conflicting concurrent updates immediately fail closed with `409 CONCURRENT_MODIFICATION`.

3. **Execution-Time Configuration Pinning**:
   - Added `config_revision_id UUID` to both `pipeline_runs` and `deployments`.
   - At pipeline trigger and deployment execution, the engine freezes `active_config_revision_id` into the record. Subsequent configuration changes do not alter in-flight or completed runs/deployments.

4. **Change Governance & Separation of Duties**:
   - Changes altering non-production configurations auto-activate immediately.
   - Any proposed revision altering production environment settings (`environment == 'prod'`) enters `pending_approval` status (`requiresApproval: True`).
   - The active pointer remains on the previous revision until approval.
   - Approval enforces separation of duties: `revision.created_by != approver` (`403 SEPARATION_OF_DUTIES`). Self-approval is strictly forbidden.
   - Rejection records the reviewer's reason and transitions status to `rejected`.

5. **Diff Viewer and Forward-Only Rollback**:
   - `GET /modules/{moduleId}/config-revisions/diff?fromRev={from}&toRev={to}` provides structural JSON path diffing between any two revisions.
   - `POST /modules/{moduleId}/config-revisions/{revisionNumber}/rollback` implements 1-click rollback: rather than reactivating an old row in-place (which would corrupt chronological history), it copies the target revision's settings forward into a brand new revision.

6. **Fail-Closed DCIM Target Revalidation & Dynamic Inventory**:
   - Added `validate_target(system_id, module_id, environment, target)` to `DcimCatalog`.
   - Immediately prior to deployment dispatch, each target host is revalidated against DCIM. If the host is marked `decommissioned`, `maintenance`, or `offline`, the deployment fails closed with `422 DCIM_TARGET_UNAVAILABLE`.
   - Added `resolve_inventory(system_id, module_id, environment)` to support dynamic host resolution from DCIM.
   - Added `server_health_records` table and `probe_server_health` queryable via `GET /servers/health`. Never reports fake `online` status on unconfigured or unreachable providers.

7. **Drift Detection (`GET /modules/{moduleId}/drift`)**:
   - Compares the active desired configuration against:
     - Running deployments (identifies active deployments still running an older revision than the desired active revision).
     - DCIM target status (identifies hosts whose DCIM state has drifted from online).

8. **Frontend User Experience (`ModulePage.tsx`)**:
   - Added `Configuration` tab to `ModulePage.tsx`:
     - Displays active revision badge and CAS config version.
     - Warning banner for pending production approvals with separation of duties enforcement.
     - Live drift detection panel highlighting discrepancies.
     - Revision timeline table with Diff viewer and Rollback actions.
     - Propose Revision modal supporting structured fields or raw JSON editing.

## Consequences
- Every configuration modification is completely auditable and traceable to an authenticated user and approver.
- In-flight deployments and historical runs are completely isolated from configuration drift.
- Production deployments can never proceed against decommissioned or maintenance hardware.
- Full compliance with SOC 2 / ISO 27001 separation of duties on production systems.
