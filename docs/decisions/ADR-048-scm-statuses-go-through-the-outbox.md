# ADR-048: SCM statuses and pull-request comments go through the outbox

Status: Accepted. Corrects ADR-019's claim that netCI reports commit statuses.

## Context

ADR-019 said netCI reports each run's status back to GitHub and GitLab. It did not.
`GitHubScmProvider.update_commit_status` and its GitLab twin read a token, logged a line,
and returned, with no HTTP request on any path. `DeliveryPlatform._notify_scm_status`
then wrote an audit record, `scm.status_updated`, every time. The audit trail stated
that the SCM had been told something it never received. The call was also made inside the
state transition's database transaction, so a slow SCM would have held a connection open.

## Decision

1. **A transition queues the report in its own unit of work.** `_notify_scm_status`
   appends an outbox row (`scm.commit_status`) and, when a pull-request run succeeds or
   fails, a second one (`scm.pr_comment`). The comment says what the build was allowed to
   do: published with its digest and not deployed, or verify-only for a fork. The audit
   record is now `scm.status_queued`, which is what is actually known at that moment.
2. **The outbox delivers it** (`adapters/scm_reporter.py`, routed by
   `RoutingNotificationDispatcher`): the GitHub statuses and issue-comments APIs, and the
   GitLab statuses and merge-request-notes APIs. Tokens are read from files
   (`NETCI_GITHUB_TOKEN_FILE`, `NETCI_GITLAB_TOKEN_FILE`), and GitHub Enterprise or a
   self-hosted GitLab are set by URL.
3. **Nothing undeliverable is marked delivered.** A missing token, an invalid
   repository, SHA or PR number, or a non-2xx response raises. The outbox retries with
   backoff and then dead-letters, and `NetciOutboxDeadLetters` (docs/SLO.md) makes that
   visible. Error messages carry the provider, the status code and part of the response
   body, never the token.

## Consequences

- Statuses arrive shortly after the transition, not during it. Ordering follows the
  outbox, which delivers oldest first.
- Only one token per provider is supported. The integration's `credentialReference` is
  carried in the payload for per-repository credentials but is not used yet.
- Not verified against real GitHub or GitLab. The lab's git server is plain HTTP and has
  no status API. Tests use an HTTP mock transport and an outbox round trip.
- Notifications to a non-URL recipient (`events@netci.local`) are still logged and
  marked delivered by the webhook dispatcher. That is a log sink, and it is noted as such.
