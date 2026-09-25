# ADR-053: CI cost is the time a run held CI, not the sum of its stages

Status: Accepted.

## Context

`GET /finops/ci` first measured "runner time" as the sum of recorded stage durations and
estimated what supersession avoided from it. Live on the lab (rc17) a `payments-api` build
held CI for 46 s while its stages summed to 17 s: the Jenkins agent pod took 22 s to start
before the first stage, and the checkout stage is reported without timings. A number
labelled "measured" was undercounting the cost by more than half, and the estimate built
on it inherited the error.

## Decision

1. **A run records when it left CI** (`ci_finished_at`, migration 0034): the first move
   out of queued/running, to a terminal state or on to approval. It is stamped where every
   run write passes (`DeliveryPlatform._apply`), not at each of the nine transitions, and
   it is written once (a `COALESCE`, and the same rule in the memory store), so a writer
   holding an older copy cannot move it.
2. **`ciSeconds` is the measure**: from `admitted_at` (dispatched to Jenkins) to
   `ci_finished_at`, over finished admitted runs. It includes the agent starting and time
   in Jenkins' own queue, which is what the capacity cost is.
3. **`stageSeconds` stays, named for what it misses.** It says where the time inside a build
   goes, not what the build cost.
4. **Nothing is backfilled.** Runs written before migration 0034 have no recorded exit;
   they are counted in `runsWithoutCiTiming`, not as zero and not from `updated_at`, which
   a later deployment moves.
5. **The estimate** of avoided time is the runs superseded before admission times the
   median `ciSeconds` of the application's runs that built their artifact. It is null when
   there is none to estimate from, and the total says when it leaves applications out.
   Money appears only with a configured price per runner hour.

## Consequences

- `ciSeconds` counts Jenkins queue time as cost. For pod agents that is close to right;
  for a static executor pool it overstates what a queued build used.
- Live evidence (2026-09-25, rc18): two `payments-api` builds held CI 45 s and 49 s,
  `ciSeconds` 93.6, against about 17 s of stages each; ten earlier runs were reported as
  `runsWithoutCiTiming`.
