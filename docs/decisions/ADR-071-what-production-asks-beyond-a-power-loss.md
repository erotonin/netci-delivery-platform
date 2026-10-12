# ADR-071: What production asks beyond a power loss -- upgrades, failing dependencies, cluster DNS, start time, a fabric that heals

- Status: accepted
- Date: 2026-10-11
- Refines: ADR-060, ADR-064, ADR-069, ADR-070

## Context

The earlier series measured one fault at a time, a machine lost. An organisation also upgrades
netCI while people build, loses its database or its BMCs' network, runs whatever DNS its
distribution ships, and restarts machines in ways that leave clocks and containers in odd states.
This round put each of those on the lab, and every one found something.

## Decisions

1. **Upgrades are a planned restart, not a failure** -- nothing changed; it is now measured.
   `lab/spike/upgrade_probe.py` rolls every component with the lab's install script while a
   netciOnce build runs, a run is submitted every 15 s and both UIs are polled.
2. **A failing dependency is refused, never pretended** -- nothing changed; now measured
   (`lab/spike/dependency_probe.py`).
3. **Cluster DNS on one machine is reported** (`NetciClusterDNSOnOneMachine`,
   `supervisor.clusterDNSService`). k3s ships one CoreDNS; the lab lost it with a cell's machine,
   and for the whole takeover nothing resolved a name: the volume moved in 60 s instead of ~15.
4. **A takeover's agents reach the controller as soon as it is up**: the jenkins Service publishes
   the pod before it is ready (Jenkins answers 503 without a session until fully up; every client
   retries), and the startupProbe asks every second.
5. **A controller no longer unzips its plugins at every start**: the image unzips them when it is
   built; the cell agent links the pod's plugin root to them and gives identical archives in
   `JENKINS_HOME` the image's times (`cellagent.LinkPluginRoot`, `AlignPluginTimes`).
6. **A broken warm sandbox does not cost a build its wait.**
   - A warm sandbox whose pod is not ready is failed and replaced.
   - A warm claim not bound within 15 s fails its sandbox; a cold one keeps 2 minutes.
   - `GET /v1/claims/{id}` tells the claiming cell what became of its claim, and the plugin's
     launcher stops as soon as it was given up.
   - A claim bound 20 s with no agent online is given up by the plugin.
7. **The fabric times claims on the database's clock**, not its own: claimed_at is the database's,
   and a machine that resumed 197 s behind made the fabric give up every claim.
   netci-queue's accept-to-start metric is now the difference of the database's own two
   timestamps too (`runqueue.Store.AcceptToStart`).
8. **CI checks the charts and the alerts** on every push (`ci/charts.sh`,
   `deploy/helm/netci/ci/test-rules.sh`).

## Verified (lab)

| What | Result | Evidence |
|---|---|---|
| Rollback, then upgrade, of every component during a 240 s netciOnce build | Build SUCCESS, no tick lost, one marker; 9/9 and 6/6 runs exactly once, no intake failure; no supervisor action, no taint; UIs down 19-42 s | `upgrade-*.json` |
| PostgreSQL restarted (20 s) | netciOnce waited and ran, one marker; every accepted run once; refusals were 503 or no ready replica | `dependency-pg-restart-*.json` |
| PostgreSQL down 90 s | netciOnce failed closed ("cannot tell whether this block ran before ... not running it"), no marker | `dependency-pg-outage-*.json` |
| BMC unreachable when a machine dies | 60 s with nothing done to the cell and an alert; fenced 0.1 s after the BMC returned; build SUCCESS | `dependency-bmc-outage-*.json` |
| Cluster DNS on one machine | Metric 1 in 40 s, alert pending at 50 s, firing at 650 s, cleared 30 s after two replicas | `cluster-dns-alert-*.json` |
| Agent after the controller is up (takeovers) | 1.5-2.6 s (6 runs), against 6.25 s before (one run) | `takeover-timelines-20261011*.json` |
| Jenkins start, gate open to fully up | Planned restart 6.96 s against 9.3-9.4 s; takeovers 8.5-10.3 s against 9.2-13.0 s | the same |
| A build past two broken warm sandboxes (frozen) | SUCCESS in 52.8 s; before the changes, each could cost 300 s (one build waited 353 s) | `fabric-broken-sandbox-*.json` |
| Soak: 3 h of random faults back to back, seed 20261012 (power loss 15, power loss + supervisor leader 7, hung machine 6, JVM killed 4, PostgreSQL restarted 4, pod deleted 2) | 38/38 rounds PASS: every netciOnce build SUCCESS with no tick lost and one marker; after each round every Lease held by its own pod, no taint left, every node Ready, every volume healthy, a supervisor leader. Fenced 2.3-3.9 s after a power loss, 16.5-20.5 s after a hang (the Lease's 15 s), ~25 s when the supervisor's leader died too; build resumed 34-67 s (median 43) | `soak-20261011T125323Z.json` and each round's own |
| The image with both clock fixes on the lab (netci@sha256:134e2db8…, rolled by `lab/helm-install.sh`) | fabric builds SUCCESS; claim to bind 0.27 s mean; accept to start 22.7 s mean for 5 runs of queue-probe-short, whose quiet period is 20 s | `fabric-timeline-20261012T023941Z.json`, Prometheus |
| Every fix | Tests that failed before and pass now: Go (supervisor, fabric on PostgreSQL, cell agent) and the plugin (JenkinsRule) | the commits |

## Found on the way

- **The host's reboots** left the machines' clocks ~7 h ahead at start (a dual-boot host keeps local
  time in its RTC): k3s issued agent certificates valid from the future and every node went
  NotReady when NTP corrected the clock. Pods then stuck in CreateContainerError, and Longhorn's
  engine-image and manager pods unready, until deleted. A lab matter, recorded in the lab notes.
- **A paused machine resumes with a stale clock** that its NTP client does not notice until its
  next poll (up to 34 min): timesyncd reported -12 ms while the machine was 197 s behind. netCI
  must not depend on clocks agreeing across machines; the supervisor never did (it times a Lease on
  its own clock), the fabric did and does not any more.
- **The host's disk filled twice** (a controller image push writes a tar first) and QEMU paused
  every machine. Nothing was fenced: paused machines report power on, and the supervisor holds
  back when every node stops at once.
- A flaky cell agent test, real fsyncs against 50 ms (CI run 37883975435), rewritten.

## Not shown

- The fabric is not faster than the Kubernetes plugin per build: median 4.7 s against 4.1 s one at
  a time, 5.6 s against 4.8 s four at a time (`fabric-timeline-20261012T022726Z.json`, cells on two
  machines, images cached). Under bursts its tail was shorter: 1 of 17 builds over 15 s against 6 of
  16; a small sample. Those slow builds of both lost ~15 s either before the agent connected or
  before the first `sh` step ran: found since (ADR-072), the controller's jars fetched by each new agent.
- Measured with both controllers on one machine (lab-3, 84% memory, after takeovers), every build
  of both took ~9 s and some 20-48 s; latency figures need the cells apart (RUNBOOK
  NetciCellsShareAMachine).
- The soak never drew the BMC-unreachable fault (weight 1 of 12); that case is measured on its own
  (`dependency-bmc-outage-*.json`). A first soak stopped at round 2 on a defect of the harness, not of
  netCI (the timestamper's prefix hid the build's ticks from the probe): `soak-20261011T113218Z.json`.

## Rejected

- **Hot reload of the plugin root instead of a link**: the chart would have to know the image; a
  link made at start works with images that do not have it.
- **A fixed grace for not-ready warm pods**: a sandbox is ready once the fabric answered it and stays
  so; not ready means its container is gone.
- **Lowering connectTimeoutSeconds**: a cold claim may take minutes to start its pod; the claim's
  state says when waiting is pointless.
