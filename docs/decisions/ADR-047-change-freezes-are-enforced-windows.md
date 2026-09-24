# ADR-047: Change freezes are enforced windows

Status: Accepted.

## Context

netCI recorded "maintenance" only as a flag on a server, true or false right now. A
change freeze, such as "no production changes during the sale" or "no deployments over
the release weekend", lived in a chat message. A deployment during one was stopped only
if someone remembered. The Release Calendar had to say that netCI knew of no future
windows.

## Decision

1. **A freeze is a record** (`change_freezes`, migration 0031): a name, `[starts_at,
   ends_at)`, the environments it covers, an optional system or module scope, a reason,
   and who created it. Cancelling a freeze is conditional, so two people cancelling at
   once record one cancellation. Release managers and platform admins create and cancel
   freezes.
2. **It is enforced where a deployment starts to move**, not where it is displayed
   (`domain/freezes.py` decides; `delivery.py` refuses):
   - a promotion or redeploy to dev or staging is refused with 409 `CHANGE_FREEZE`;
   - a production deployment is refused when it is approved, the moment it would start;
   - a production request whose `scheduledFor` falls inside a prod freeze is refused
     when it is created. The person planning the release learns this today, not at
     approval;
   - **a CI result during a freeze is still recorded.** The build succeeds as build-only,
     with a log line naming the freeze. Refusing the callback would lose the build's
     result, and the artifact can be promoted once the freeze ends.
3. **Rollbacks are never refused.** A freeze exists to protect service, not to prevent
   restoring it.
4. **The way through is a break-glass for that freeze** (`target_type = change_freeze`,
   `target_id = <freeze id>`). It uses the existing break-glass flow and its dual
   control, so an exception is granted for one freeze, by someone other than the
   requester, and expires.

## Consequences

- A production deployment approved before a freeze and scheduled to start inside it
  (`notBefore`) is not stopped. The workflow sleeps until then, and the worker has no
  access to the freeze records. The refusal at production request creation covers the
  common case. Checking again in the workflow is the fix, and it is not built.
- Freezes are shown on the Release Calendar, and a request scheduled inside one is
  flagged there.
