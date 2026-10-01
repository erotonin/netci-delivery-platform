# netCI — what it is, what it has shown, what it has not

## The problem

Most organisations run Jenkins as one controller per team or business unit. When a
controller's machine dies:
- every running build fails or hangs;
- everything waiting in its queue is lost: Jenkins keeps the queue in memory
  (JENKINS-30909, reproduced in our lab);
- webhooks sent during the outage are lost;
- recovery is a person restoring a backup.

Deployments run from the same Jenkins, so a deploy cut in half is a production incident.

CloudBees CI's high availability addresses this, under a commercial licence. netCI does it for
open-source Jenkins, around unmodified controllers: teams keep their Jenkinsfiles, plugins and
UI.

## What it does

| Part | What it does |
|---|---|
| **Cell agent** (sidecar) | The controller runs only while it holds its cell's Lease. Losing the Lease kills it within the renew deadline, so two controllers never write one `JENKINS_HOME`. |
| **Cell Supervisor** | Notices a controller that stopped renewing and confirms its machine is off through the machine's power controller (Redfish BMC, or an SSH power agent for virtual machines). It then releases the Lease and has Kubernetes start the controller on another machine, on the same replicated volume. It never acts on a guess: an unknown power state, or a mass failure, gets an alert instead. |
| **Durable run queue** (`netci-queue`) | Webhooks (GitLab, GitHub) and API triggers are written to PostgreSQL before they are acknowledged. The netCI plugin hands them to Jenkins idempotently, so a run the controller lost is handed over again and never runs twice. |
| **`netciOnce`** | A Pipeline block that must not run twice (deploy, migrate, publish) is recorded outside `JENKINS_HOME`. After a takeover it is refused rather than repeated. |
| **Agent fabric** | Build sandboxes on Kubernetes-on-VMs, one per build: user namespace, no API credentials, limits. Warm sandboxes are bound late to whichever controller claims them. |

## What the lab has shown

The lab is three KVM machines running k3s and Longhorn, with real Jenkins 2.555.3. Evidence is
in `lab/evidence/`, and every row below names its source in ADR-060, 063 or 064.

| Measured | Result |
|---|---|
| Power cut under a running 240 s Pipeline build, no person involved | Build resumed and finished SUCCESS every time (14 runs). Steps were never re-run; at most 1 log line was lost. |
| Time from power loss to the build continuing | 71-84 s over 4 repeated runs (machine confirmed off and fenced at 14.5-23 s), 42.5 s at best. The rest is the storage attaching the volume elsewhere (13-34 s) and Jenkins starting (~15 s). |
| A healthy cell on another machine during the failure | Kept its Jenkins, after the Lease timings were fixed (a 4 s deadline did not outlast an etcd stall) |
| Controller JVM killed with 6 builds queued | The 5 netCI runs each ran once; the 1 direct trigger was lost |
| Real GitLab push, then GitLab resending the same delivery | One run and one build, 8.9 s from push to the build finishing |
| Sandbox isolation | Container root mapped to an unprivileged host uid |

## What it has not shown yet

- **Production scale:** many cells, hundreds of builds at once, and the organisation's
  hypervisor and storage. The lab is three VMs on one host.
- **Faster builds:** on an idle lab the fabric is not faster than the Kubernetes plugin.
- **Logs and history readable during a takeover:** not built (planned on OpenSearch).
- **Fleet autoscaling and host recycling:** not built.

## How it would be introduced

1. One cell, beside the existing controllers. Jobs move to it folder by folder, from JCasC and
   Job DSL in git.
2. Webhooks repointed to netci-queue for those jobs. Nothing else in GitLab changes.
3. The supervisor given a power controller for the cell machines: Redfish credentials (read
   and power only), or an API account for the virtualisation platform.
4. Failure drills, as in `lab/spike/chaos_poweroff.py`, on the organisation's own
   infrastructure before any team depends on it.
