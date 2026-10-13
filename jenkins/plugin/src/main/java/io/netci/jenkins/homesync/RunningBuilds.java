package io.netci.jenkins.homesync;

import hudson.Extension;
import hudson.FilePath;
import hudson.init.InitMilestone;
import hudson.init.Initializer;
import hudson.model.Job;
import hudson.model.Run;
import hudson.model.TaskListener;
import hudson.model.listeners.RunListener;
import hudson.security.ACL;
import hudson.security.ACLContext;
import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.IOException;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.TimeUnit;
import java.util.logging.Level;
import java.util.logging.Logger;
import jenkins.model.Jenkins;
import jenkins.model.lazy.LazyBuildMixIn;
import jenkins.util.Timer;

/**
 * A running build survives its replica (ADR-079).
 *
 * <p>While it runs, the owner publishes a copy of its directory every few seconds, under
 * {@code @running/<job>/<n>}; the other replicas only record that the copy exists. When the job's
 * lease has expired and the fence says its owner is not running, a replica takes the lease, writes
 * the latest copy into its home and loads the run -- which resumes itself (loading is the whole
 * adoption; a second resumption failed the build by hand). A replica that starts moves its own
 * unfinished builds aside first, so they too come back only through adoption: a build is resumed
 * in one place.
 */
public final class RunningBuilds {
    private static final Logger LOGGER = Logger.getLogger(RunningBuilds.class.getName());
    static final String RUNNING = "@running/";
    static final long EVERY_SECONDS = 3;
    static final String SUSPECT = ".netci-suspect-";

    private static final Map<String, ScheduledFuture<?>> PUBLISHERS = new ConcurrentHashMap<>();

    private RunningBuilds() {}

    static String path(Run<?, ?> run) {
        return RUNNING + run.getParent().getFullName() + "/" + run.getNumber();
    }

    static byte[] archive(File dir) throws IOException, InterruptedException {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        try (OutputStream gz = FilePath.TarCompression.GZIP.compress(out)) {
            // Not the temporary files of an atomic write in progress: one vanished mid-archive and
            // the copy failed ("closed at 0 before the 39 bytes specified in the header").
            new FilePath(dir).tar(gz, new hudson.util.DirScanner.Glob("**/*", "**/*-atomic*tmp"));
        }
        return out.toByteArray();
    }

    /**
     * Whether a build directory holds a build that had not finished. A running Pipeline's build.xml
     * already says {@code <result>SUCCESS} (its initial value) next to {@code <completed>false}: a
     * missing result alone, the first test, found no running build at all.
     */
    static boolean unfinished(String buildXml) {
        if (buildXml.contains("<completed>")) {
            return buildXml.contains("<completed>false</completed>");
        }
        return !buildXml.contains("<result>");
    }

    /** Publish this run's copy every few seconds while it runs (nothing is sent when it did not change). */
    static void startPublishing(Run<?, ?> run) {
        HomeSync sync = HomeSync.get();
        if (sync == null) {
            return;
        }
        String path = path(run);
        PUBLISHERS.computeIfAbsent(path, p -> Timer.get().scheduleWithFixedDelay(() -> publishCopy(sync, run),
                0, EVERY_SECONDS, TimeUnit.SECONDS));
    }

    /**
     * One copy, if it can be adopted: a run's directory before its build.xml is written is a log and
     * nothing else -- the first test's only copy, which no replica could load.
     */
    static void publishCopy(HomeSync sync, Run<?, ?> run) {
        if (!run.isBuilding() || !new File(run.getRootDir(), "build.xml").exists()) {
            return;
        }
        {
            try {
                sync.publish(path(run), archive(run.getRootDir()), 0);
            } catch (IOException e) {
                LOGGER.log(Level.WARNING, "netCI homesync: cannot copy the running " + run, e);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
        }
    }

    /**
     * A replica stopping on purpose (an upgrade, a restart) publishes a last copy of every running
     * build, so whichever replica adopts it continues from that moment, not from up to 3 s before.
     */
    @hudson.init.Terminator
    public static void lastCopies() {
        HomeSync sync = HomeSync.get();
        if (sync == null) {
            return;
        }
        try (ACLContext ignored = ACL.as2(ACL.SYSTEM2)) {
            for (Job<?, ?> job : Jenkins.get().getAllItems(Job.class)) {
                for (Run<?, ?> run : job.getBuilds()) {
                    if (run.isBuilding()) {
                        publishCopy(sync, run);
                    }
                }
            }
        }
    }

    @Extension
    public static final class Copy extends RunListener<Run<?, ?>> {
        @Override
        public void onStarted(Run<?, ?> run, TaskListener listener) {
            HomeSync sync = HomeSync.get();
            if (sync != null && sync.ready()) {
                startPublishing(run);
            }
        }

        @Override
        public void onFinalized(Run<?, ?> run) {
            ScheduledFuture<?> f = PUBLISHERS.remove(path(run));
            if (f != null) {
                f.cancel(false);
            }
            HomeSync sync = HomeSync.get();
            if (sync != null && sync.ready() && sync.versions.containsKey(path(run))) {
                sync.publishDelete(path(run), 0);
            }
        }
    }

    /**
     * Before Jenkins loads its jobs, this replica's unfinished builds are moved aside: the store
     * holds their latest copy, and adoption brings each back in exactly one place. Before, not
     * after: a job lists its build numbers as it loads, and a number listed while its directory was
     * missing stayed unloadable -- adopting it then found nothing (the second version's test).
     */
    @Initializer(after = InitMilestone.EXTENSIONS_AUGMENTED, before = InitMilestone.JOB_LOADED)
    public static void setAsideUnfinished() {
        if (HomeSync.setting("NETCI_HOMESYNC_URL", "") == null) {
            return;
        }
        setAsideIn(new File(Jenkins.get().getRootDir(), "jobs"), "");
    }

    /** jobs/<name>/builds/<n>, and folders' jobs/<name>/jobs/... below them. */
    private static void setAsideIn(File jobs, String prefix) {
        File[] items = jobs.listFiles(File::isDirectory);
        for (File item : items == null ? new File[0] : items) {
            String name = prefix + item.getName();
            setAsideIn(new File(item, "jobs"), name + "/");
            File builds = new File(item, "builds");
            File[] dirs = builds.listFiles(f -> f.isDirectory() && f.getName().matches("[0-9]+"));
            for (File dir : dirs == null ? new File[0] : dirs) {
                File xml = new File(dir, "build.xml");
                try {
                    if (xml.exists() && unfinished(Files.readString(xml.toPath(), StandardCharsets.UTF_8))) {
                        File aside = new File(builds, SUSPECT + dir.getName());
                        new FilePath(aside).deleteRecursive();
                        if (!dir.renameTo(aside)) {
                            throw new IOException("cannot move " + dir + " aside");
                        }
                        LOGGER.info(() -> "netCI homesync: " + name + " #" + dir.getName()
                                + " was running when this replica stopped; it returns through adoption, not by itself");
                    }
                } catch (IOException e) {
                    LOGGER.log(Level.SEVERE, "netCI homesync: cannot set aside " + dir, e);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                    return;
                }
            }
        }
    }

    /** One pass: adopt every running build whose owner's lease expired and whose owner the fence says is gone. */
    static void adoptOrphans(HomeSync sync) {
        Fence fence = Fence.configured();
        for (Map.Entry<String, Long> e : sync.runningCopies.entrySet()) {
            String path = e.getKey();
            int slash = path.lastIndexOf('/');
            String jobName = path.substring(RUNNING.length(), slash);
            int number = Integer.parseInt(path.substring(slash + 1));
            try (ACLContext ignored = ACL.as2(ACL.SYSTEM2)) {
                Job<?, ?> job = Jenkins.get().getItemByFullName(jobName, Job.class);
                if (job == null) {
                    continue;
                }
                Run<?, ?> here = job.getBuildByNumber(number);
                if (here != null) {
                    if (!here.isBuilding()) {
                        sync.runningCopies.remove(path); // finished: its copy is about to go
                    }
                    continue; // running here already
                }
                HomeSyncClient.JobOwner owner = sync.client.jobOwner(jobName);
                boolean mine = owner.owner().equals(OwnerRouting.me());
                if (owner.live() && !mine) {
                    continue; // its owner is alive
                }
                // This replica's own build (it restarted: the old process is gone), or another's whose
                // lease expired and that the fence says is not running.
                if (!mine && (fence == null || !fence.isGone(owner.owner(), System.currentTimeMillis() + owner.expiresInMs()
                        - BuildOwnership.LEASE.toMillis()))) {
                    continue;
                }
                if (!BuildOwnership.own(sync, job)) {
                    continue; // someone else took it first
                }
                HomeSyncClient.Item copy = sync.client.get(path);
                if (copy.content() == null) {
                    sync.runningCopies.remove(path);
                    continue;
                }
                // The build follows its agent: adopted only where the agent it was using is online.
                // A replica that came back fast took its own build while its agent had reconnected to
                // the other replica, and the build waited for that agent for good (the lab's first run).
                String agent = agentOf(copy.content());
                if (agent != null) {
                    hudson.model.Computer c = Jenkins.get().getComputer(agent);
                    if (c == null || c.isOffline()) {
                        continue;
                    }
                }
                File aside = new File(job.getBuildDir(), SUSPECT + number);
                if (mine && new File(aside, "build.xml").exists() && new File(aside, "program.dat").exists()) {
                    // This replica's own build, set aside at its start: its own last state on disk is
                    // at least as new as the store's copy.
                    adopt(job, number, archive(aside));
                } else {
                    adopt(job, number, copy.content());
                }
                sync.runningCopies.remove(path);
            } catch (IOException ex) {
                LOGGER.log(Level.WARNING, "netCI homesync: could not adopt " + path, ex);
            } catch (InterruptedException ex) {
                Thread.currentThread().interrupt();
                return;
            } catch (RuntimeException ex) {
                LOGGER.log(Level.SEVERE, "netCI homesync: adopting " + path + " failed", ex);
            }
        }
    }

    /**
     * The agent a build is using, from its copy: the last "Running on X in" of its log, unless the
     * node block it opened has closed since. Null when it is on no agent now.
     */
    static String agentOf(byte[] copy) throws IOException, InterruptedException {
        File tmp = Files.createTempDirectory("netci-copy").toFile();
        try {
            new FilePath(tmp).untarFrom(new ByteArrayInputStream(copy), FilePath.TarCompression.GZIP);
            File log = new File(tmp, "log");
            return log.exists() ? agentInLog(Files.readString(log.toPath(), StandardCharsets.UTF_8)) : null;
        } finally {
            new FilePath(tmp).deleteRecursive();
        }
    }

    static String agentInLog(String log) {
        java.util.regex.Matcher m = java.util.regex.Pattern.compile("Running on (\\S+) in ").matcher(log);
        String agent = null;
        int at = -1;
        while (m.find()) {
            agent = m.group(1);
            at = m.end();
        }
        return agent != null && log.indexOf("[Pipeline] // node", at) < 0 ? agent : null;
    }

    static void adopt(Job<?, ?> job, int number, byte[] copy) throws IOException, InterruptedException {
        File dir = new File(job.getBuildDir(), Integer.toString(number));
        new FilePath(new File(job.getBuildDir(), SUSPECT + number)).deleteRecursive(); // the stale local copy, if any
        new FilePath(dir).deleteRecursive();
        if (!dir.mkdirs()) {
            throw new IOException("cannot create " + dir);
        }
        new FilePath(dir).untarFrom(new ByteArrayInputStream(copy), FilePath.TarCompression.GZIP);
        if (job.getNextBuildNumber() <= number) {
            job.updateNextBuildNumber(number + 1);
        }
        if (job instanceof LazyBuildMixIn.LazyLoadingJob<?, ?> lazy) {
            lazy.getLazyBuildMixIn()._getRuns().recognizeNumber(number);
        }
        Run<?, ?> run = job.getBuildByNumber(number); // loading it resumes it
        if (run == null) {
            String[] files = dir.list();
            Object keys = job instanceof LazyBuildMixIn.LazyLoadingJob<?, ?> lazy2 ? lazy2.getLazyBuildMixIn()._getRuns().keySet() : "?";
            throw new IOException(job.getFullName() + " #" + number + " did not load from its copy; dir " + java.util.Arrays.toString(files)
                    + ", known numbers " + keys + ", base " + job.getBuildDir());
        }
        LOGGER.info(() -> "netCI homesync: adopted " + run + " (building: " + run.isBuilding() + ")");
        if (run.isBuilding()) {
            startPublishing(run);
        }
    }
}
