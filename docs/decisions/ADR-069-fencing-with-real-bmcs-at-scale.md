# ADR-069: Fencing with real BMCs, at scale, without costing a running build

- Status: accepted
- Date: 2026-10-09
- Refines: ADR-060 (cells, takeover)

## Context

Three things the lab could not show, because it has three machines, VMs, and a Redfish
emulator that answers at once and always succeeds:

1. **Real BMCs.** They are slow (0.5-3 s a request), busy at times (503), and late: PowerState
   still says On seconds after a ForceOff. iDRAC answers 409 "Server is already powered OFF" when
   the machine lost its power between a state read and the ForceOff (Red Hat bug 1873305), and
   refuses with 409 for a while after a power change (openshift/openstack-ironic#502). OpenBMC
   names its allowed reset types through `@Redfish.ActionInfo`, not inline.
2. **Scale.** An organisation runs tens to hundreds of cells, on as many machines.
3. **A full cluster.** A controller that needs room after a takeover preempts pods of lower
   priority -- by design, or a takeover could be stuck -- and in the lab one preempted the agent
   of the very build it was resuming.

## Decision

1. **A failed power-off is not the end of fencing.** `fence.EnsureOff` keeps asking for the
   state until its deadline and sends the ForceOff again every 2 s; only a refusal
   (400/401/403/404/405) ends it at once. As before, nothing counts as off until the BMC says
   Off. The timeouts are settings: `supervisor.power.queryTimeout` (3 s), `offTimeout` (60 s,
   as Ironic's `power_state_change_timeout`; it was 20 s), and Redfish's `requestTimeout` in
   `fence.json`. A longer `offTimeout` costs nothing when a BMC is quick.
2. **An observation costs the same for any number of cells.** The supervisor lists every
   cell's Lease and pod by name across namespaces, one request each, instead of one GET of each
   per cell. `netci_supervisor_observation_seconds` and `NetciSupervisorSlowObservations` watch it.
3. **Running builds carry a disruption budget.** netci-fabric labels a claimed sandbox
   `netci.io/busy`; the charts put a budget on that label and on the Kubernetes plugin's agents.
   kube-scheduler prefers victims whose budget preemption would not break, and still preempts
   when nothing else frees room. The budget is an integer `minAvailable` above any pod count:
   Kubernetes defines budgets on pods without a controller only in that form (with
   `maxUnavailable` the disruption controller warns "undefined behavior").

4. **A changed fence configuration is loaded and checked.** The supervisor reads it once; it
   now fingerprints its directory (a Secret volume's `..data` link) and starts again when it
   changes, so a rotated BMC password or a new machine goes through the start-up mapping check.

## Verified

| What | How | Result |
|---|---|---|
| BMC behaviour, unit | `internal/fence/bmc_quirks_test.go`: 409 already off, a late answer, 503, 409 while settling, a refusal, OpenBMC's ActionInfo | The old `EnsureOff` failed 4 of them; all pass |
| BMC behaviour, live | `lab/redfish.sh quirks` in front of the emulator: 1.5 s a request, first ForceOff 503, PowerState 8 s late; 3 hung machines (`chaos-poweroff-20261009T020225Z.json`) | 3/3 SUCCESS. Confirmed off 22 s after fencing began, past the old 20 s timeout: hence 60 s |
| Scale | `lab/scale/run.sh` on kwok (real kube-apiserver, etcd, scheduler, controller manager v1.36.1); the real lease code for each cell agent; the real supervisor | Before: an observation took 4.0 s at 100 cells and 11.9 s at 300, and fencing 6-8 s and 12-17 s. After: 0.05-0.1 s mean at 100-600 cells, fencing 2.6-3.0 s, every cell held again within 5.7 s; 0.16 cores and 53 MiB at 600 cells |
| Preemption | `lab/scale/preemption.py`, real kube-scheduler | Without the budget a running build was preempted 5/5; with it 0/5, the idle sandbox taken instead; with builds on every machine the controller was still placed 2/2 |
| The lab after the change | 3 power losses through the same slow BMC (`chaos-poweroff-20261009T040127Z.json`) | 3/3 SUCCESS, one netciOnce marker each. Observations 17 ms p50, 84 ms p99 when quiet. Fencing took 5.5-6.9 s against 2.8-4.1 s with the emulator: the BMC's 1.5 s twice, once for the state read that prompts the fencing and once for the confirmation before the Lease is released -- the confirmation is kept |
| A rotated BMC password | The emulator's password changed, then the fence Secret | Before: the replicas kept the old configuration (found when re-pointing them). After: each stopped when its volume was updated, 18 s apart, and checked the mapping with the new password; both within 66 s of the Secret. A power loss then: fenced 3.0 s, SUCCESS (`chaos-poweroff-20261009T042237Z.json`) |
| Budget on the lab | a claimed sandbox | Labelled at once; its eviction refused by the budget, an idle one's allowed |
| The alert | `lab/spike/headroom_probe.py` | Metric 0 after 26 s, alert firing at the rule's 10 minutes, cleared 30 s after the build ended |

## What is still not verified

- **Real BMCs.** The behaviours above are reproduced from vendors' documented and reported
  behaviour; nothing has fenced a physical server. Before production, run the drill in
  `docs/INSTALL.md` against your BMCs.
- **Scale with real storage and Jenkins.** The scale test measures the control plane's share
  (detection, fencing, the Lease); a takeover in the lab spends ~22 s moving the volume and ~17 s
  starting Jenkins, out of ~51 s (chaos series 19). Neither depends on the number of cells, but
  a storage system moving many volumes at once is untested. *Since measured (ADR-070): eight
  Longhorn volumes moved as fast as one; the first pod deleted paid ~10 s, now avoided.*

## Rejected

- **Informers instead of lists.** A watch on a dead API server is a stale view that looks
  fresh; ADR-066's clients bound each request instead. Two lists a second cost the API server
  little: they are served from its watch cache (ConsistentListFromCache, on by default since
  1.31, GA in 1.34; KEP-2340). On older clusters each is a read of etcd.
- **Retrying ForceOff without a deadline.** A machine that never reports Off must reach a person.
- **Never preempting builds.** A takeover with no other room would wait for builds to finish,
  and every build of that cell with it.

## Consequences

- Draining a node waits for the builds on it (`kubectl drain --disable-eviction` overrides).
- The supervisor needs `list` on pods and leases cluster-wide (it already had it).
