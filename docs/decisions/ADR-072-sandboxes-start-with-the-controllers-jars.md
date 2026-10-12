# ADR-072: Sandboxes start with the controller's jars in their agent's cache

- Status: accepted
- Date: 2026-10-12
- Refines: ADR-064 (agent fabric), ADR-071

## Context

ADR-071 left a question open: some builds, on the fabric and on the Kubernetes plugin alike, lost
~15 s before their agent was online or before their first `sh` step printed anything, and nobody
could say why, because the agents' pods were gone by the time anyone looked.

`lab/spike/agent_delay_probe.py` follows every agent pod's log from the moment it exists, the
controller's log, and when the controller first calls each agent online. It found:

- An agent that had said "Connected" was online on the controller 15-16 s later in some builds of
  both (2 of 16 fabric, 2 of 16 Kubernetes plugin).
- A thread dump of the controller taken while one was stuck: `SlaveComputer.setChannel` waited on
  a call to the agent, and another thread was writing a jar to the agent (`JarLoaderImpl.writeJarTo`),
  blocked in `PipeWindow.get`, i.e. waiting for the agent to take more of it.
- A fresh agent fetches ~19 MB of the controller's jars over its channel: 15 jars, jenkins-core
  alone 11.9 MB, the rest core libraries and the plugins of the steps (durable-task for `sh`). Its
  jar cache is empty in every new pod, so every sandbox fetches them again; four at once share the
  controller's channels, and the window fills.
- The `sh` step (durable-task) polls for output (`USE_WATCHING` is off by default), and its first
  check waits for those classes too.

## Decision

1. **An image holds the agent's jar cache for one controller image** (`jenkins/Dockerfile.agent-jars`
   from `jenkins/agent-jars/agent-jars.txt`): the listed jars, laid out under the names remoting asks
   its cache for, computed by remoting's own `Checksum` and `FileSystemJarCache` against the
   controller's remoting. A line that does not match exactly one file stops the build.
2. **A fabric pool's `jarCache`** names that image, pinned by digest. It is mounted read-only, and
   `netci-sandbox` copies it into the agent's own cache before the sandbox is warm, so no claim waits
   for it. Only entries named as remoting names them are copied, never a link, never over a file;
   a missing cache only means the agent fetches what it needs, as before.
3. Trust: the agent loads a cached jar by its name and never checks its content
   (`FileSystemJarCache.lookInCache`). The cache therefore comes only from an image the platform
   built, pinned by digest and read-only. Each sandbox gets its own copy: a build that alters it
   alters only its own agent, which it already controls. Nothing a sandbox wrote is ever given to
   another.

## Verified (lab)

| What | Result | Evidence |
|---|---|---|
| Where the time went | As above: 15-16 s from "Connected" to online, the controller writing a jar to the agent meanwhile | `agent-delay-20261012T030531Z.json` and its directory (logs, thread dump) |
| Warm sandboxes seeded by hand against none, alternating, 16 builds each | 3.38 s median, 4.04 s at worst, none stalled; against 4.66 s, 18.59 s, 2 stalled 15 s | `agent-jar-cache-ab-20261012.json` |
| The image's cache against what a real agent fetched | The same 15 names and contents | the build, compared by hand |
| On the lab with the feature (netci@sha256:ab2d7a77…, jenkins-agent-jars@sha256:c4685316…) | Seeded 15 jars, 19.5 MB, in 9 ms, in user-namespaced pods. Fabric: 3.88 s one at a time (2.31-4.79), 4.82 s four at a time (3.63-5.97), no build over 6 s of 23; before, 4.73 s and 5.6 s with one at 20.5 s. The Kubernetes plugin in the same run: 3.17 s (one at 21.1 s) and 6.17 s | `fabric-timeline-20261012T034419Z.json` |
| Tests | Go: the pool's image mounted read-only and named to the bootstrap, refused by tag; seeded before warm, odd names, links and existing files left alone; a missing cache still runs. Each fails without its code | the commit |

## Not shown

- The list is what one agent fetched for a Pipeline with `sh` steps. Steps of other plugins (git,
  credentials, artifacts) fetch their own jars, which are not in it: they are fetched as before.
- The Kata pool has no jar cache: image volumes were not tried with Kata.
- One at a time, the fabric is still ~0.7 s behind the Kubernetes plugin's median, which itself
  moved between runs (4.06 s, 3.17 s); small samples.
- The probes authenticate with a password, which Jenkins checks with bcrypt on every request: the
  controller does that work during the measurements, for both kinds of agent alike.
- The Kubernetes plugin could use the same image as a volume in its pod templates; not done.

## Rejected

- **A cache shared between sandboxes, filled by them**: a build could put a jar under another's
  name and have the next agents load it.
- **Mounting the image as the agent's cache directly**: the agent writes the jars it misses into
  its cache, and a read-only one would fail them.
- **Every jar of the controller** (~200 MB): ten times the copy for jars no agent was seen fetching.
- **Turning durable-task's watching on**: it changes how every step's output reaches Jenkins, for a
  wait whose cause was the jars, not the polling.
