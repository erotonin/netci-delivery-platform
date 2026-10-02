# ADR-068: Build logs readable during a takeover, shipped beside the controller

- Status: accepted
- Date: 2026-10-02
- Refines: ADR-060 (cells, takeover)

## Context

While a cell is taken over -- about a minute in the lab -- its controller does not answer and its
`JENKINS_HOME` volume is moving to another machine. Nobody can read a build's log, including the
log of the build that was running, which is the one people want to read. CloudBees' active/active
HA does not have this gap: another replica serves the UI. netCI's README listed "history and
logs readable during a takeover" as not built.

Two ways to get the log out of the cell were tried on the lab.

**The Jenkins OpenTelemetry plugin** (3.1603.ve3fa_cc8a_b_f5e) can store Pipeline logs in Loki
through an OpenTelemetry collector, "mirrored" so Jenkins keeps its own copy. Measured:
- every fresh build agent waited the plugin's 10 s pre-online timeout for its SDK to be
  configured over remoting, also when the plugin's exporters were `none`. Builds on warm
  sandboxes went from ~10 s to 19-27 s;
- the output of `sh` steps never reached Loki. Agents send their lines themselves, through the
  SDK whose configuration had just been cancelled at the timeout. Only the controller's own
  lines ("Running on ...") arrived;
- its SDK settings take effect at the controller's start only, not on a JCasC reload.

**A log shipper beside the controller.** Jenkins writes every build's log -- the agents' output
included -- to `jobs/.../builds/N/log` on the volume. Fluent Bit in the controller's pod reads
those files as they grow and pushes the lines to Loki.

## Decision

1. **netci-cell `logShipping`**: Fluent Bit (5.1.3, pinned by digest) as a regular container in
   the controller's pod.
   - Reads jobs, jobs in folders, multibranch branches and those in folders.
   - Strips Jenkins' console notes (`ESC[8mha:...ESC[0m`).
   - Loki labels `cell` and `job`; the build number is structured metadata, not a label (a
     label per build would make a stream per build).
   - Its offsets are in `JENKINS_HOME/.netci-logship.db`, on the volume: the next controller's
     shipper goes on from where this one stopped.
2. **At least once, never lost.** A power loss can take the volume's last write-back, which
   holds both the end of the log and the shipper's offset. Jenkins writes those lines again on
   resume, and the next shipper sends them again: a line of the last second before a crash can
   be in Loki twice. Exactly once would need an identity per line, which Jenkins' log does not
   have.
3. **Loki must take pushes of ~5.3 MB.** One push is one Fluent Bit chunk (~2 MB), larger as
   Loki's protobuf. Over Loki's default 4 MB gRPC limit a backlog -- a first install, or the
   lines kept while Loki was down -- was refused with 500 and retried forever. `INSTALL.md`
   lists the settings (`grpc_server_max_recv_msg_size` 16 MB, a burst to match).
4. **The OpenTelemetry plugin is not installed.** Reverted from the controller image. Rolling
   back showed a second problem, now fixed: the Jenkins image adds its plugins to `JENKINS_HOME`
   and never removes one, so the plugin stayed, configured, and agents still waited 10 s. The
   cell agent's guard now removes, under the Lease, the plugins the image does not carry
   (`pluginsFromImageOnly`, on by default; ADR-059's drift).

## Verified on the lab

`lab/spike/logs_during_takeover.py`: a 240 s build, its controller's machine powered off, and
Loki and Jenkins asked every second. Evidence: `lab/evidence/logs-during-takeover-*.json`.

Every run, in order; the failures are what changed the design.

| Crash at tick | Verdict | What it showed | Change |
|---|---|---|---|
| 40 | FAIL | During the ~65 s outage Loki returned ticks 1-41. Afterwards all 240; tick 41 twice (Jenkins' own log: once) | Point 2: a line at the crash may be there twice |
| 25 | PASS | Ticks 1-26 readable during the outage; all 240 afterwards, 26 twice | -- |
| 70 | FAIL | The machine lost also ran Loki: nothing readable during the outage; 5 lines arrived minutes after the build (Fluent Bit's backoff had grown while Loki was down) | Retries capped at 30 s; the test waits for Loki to catch up |
| 40 | FAIL | Loki on the lost machine again: nothing readable | Loki moved to its own failure domain (the host, in the lab) |
| 70 | PASS | Ticks 1-71 readable during the outage | -- |
| 100 | FAIL | The build failed: the new controller preempted the agent running it. Not a log fault | Headroom no longer counts a running build's room (RUNBOOK, `NetciCellWithoutHeadroom`) |
| **30, 70, 110** (Loki on the host) | **3/3 PASS** | Builds SUCCESS; Jenkins down 50-95 s after the power-off; throughout, Loki returned every tick up to the crash; all 240 in Loki as the build finished; 1-2 lines twice, at the crash | -- |

## Rejected

- **The OpenTelemetry plugin's log storage**: measured above.
- **A Jenkins log storage plugin that writes only elsewhere** (JEP-210, e.g. CloudWatch): the
  build log would leave the volume, and Pipeline's resume and Jenkins' own UI would then depend
  on that service being up. Shipping a copy changes nothing in Jenkins.
- **A read-only replica of `JENKINS_HOME`**: the volume is ReadWriteOnce and moving during the
  very window it would be needed.

## Consequences

- During a takeover, a build's log is read in Grafana (Explore, Loki, `{cell="...", job="..."}`,
  then `| build="N"`), not in Jenkins.
- Run history beyond the logs: runs submitted through netci-queue are in PostgreSQL and
  readable through its API; builds started inside Jenkins are visible as their logs' streams.
- One more container per cell (~30 MB). A shipper that fails costs the Loki copy, never the
  build: Jenkins' own log is untouched.
