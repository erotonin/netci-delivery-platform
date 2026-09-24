# ADR-046: Releases are verified on metrics netCI reads itself

Status: Accepted.

## Context

Two things decided whether a release was good, and neither looked at how it behaved
under traffic:

- **After a deployment**, the runtime health gate (the playbook's own check) proves the
  process started and answers its health URL. A release that returns 500 to a third of
  real requests passes it.
- **A canary step** was decided by `CanaryAnalyzer.evaluate(metrics)`, where `metrics`
  came from the **request body**. The portal's "Advance" button sent none, and
  `evaluate({})` read that as 0 % errors and 0 ms latency. Every canary in the portal
  "passed an analysis" of no data, and any caller could send the numbers it wanted.

## Decision

1. **A module declares its verification** in `pipelineConfig.verification`: one or two
   PromQL queries (`errorRate`, `p95LatencyMs`), thresholds, a window, an interval, and
   the environments it applies to. It is validated when the configuration is written
   (`domain/verification.py`): placeholders are limited to `{release}`, `{environment}`
   and `{track}`, and the values substituted for them must be DNS labels, checked with
   `fullmatch`, because they are pasted into PromQL label matchers.
2. **Post-deploy verification is a workflow step.** When the health gate passes and the
   deployment carries a verification spec (a server-managed parameter a caller cannot
   supply; `build_inputs` refuses the key), the worker's `verify_release` activity samples
   the queries every interval for the window, and stops at the first breach. A breach, or
   a metric that returned **no data for the whole window**, takes the same path as a
   failed health check: automatic rollback, or `failed` for manual intervention. The step
   is behind `workflow.patched("post-deploy-verification-v1")`, so workflows started
   before it replay the history they recorded.
3. **Canary analysis reads Prometheus on the server**, with the same queries and
   `{track} = canary`. A request carrying metrics is refused (422). When netCI cannot
   analyse (no queries, no Prometheus, no data), the step is refused (409), and neither
   advanced nor aborted, because "cannot tell" is not "bad". A reviewer may advance it with
   an `overrideReason`. That is audited as `canary.advanced_without_analysis` and returned
   as `analysed: false`, never as a pass.
4. `NETCI_PROMETHEUS_URL` (chart: `verification.prometheusUrl`) configures both the API
   and the worker. When it is unset, every query raises, so the fail-closed behaviour
   above applies instead of a silent skip.

## Consequences

- A module that declares verification without its metrics existing in Prometheus rolls
  back every release. That is intended: the declaration says "judge me on these metrics",
  and none were there to judge.
- A module that declares nothing is deployed as before, and the deployment message does
  not say "verified".
- Verification lengthens a deployment by up to its window (1 to 60 minutes). The lease
  is held and heartbeated for that time.
- The thresholds a production request used to carry (`strategyConfig.thresholds`) no
  longer decide anything. The module's verification spec does.
