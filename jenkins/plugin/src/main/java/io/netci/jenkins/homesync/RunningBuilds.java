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

    /**
     * One pass: adopt every running build whose runner is gone and whose agent is here.
     *
     * <p>The runner is the replica that last wrote the build's copy -- not the job's lease holder:
     * the lease decides who starts builds and reserves numbers, and it can move to another replica
     * while an orphan waits (the lab: r1 took the lease to build #8 while #7's agent was on r0, and
     * #7 was stuck between them). Adopted when: the runner is this replica (it restarted: the old
     * process is gone) or the fence says it is gone since its last copy; and the agent the build is
     * on is online here (or it is on none). The claim is a compare-and-set of the copy under this
     * replica's name: of two replicas trying at once, one wins.
     */
    static void adoptOrphans(HomeSync sync) {
        Fence fence = Fence.configured();
        String me = OwnerRouting.me();
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
                HomeSyncClient.Item copy = sync.client.get(path);
                if (copy.content() == null) {
                    sync.runningCopies.remove(path);
                    continue;
                }
                boolean mine = copy.updatedBy().equals(me);
                if (!mine && (fence == null || !fence.isGone(copy.updatedBy(), copy.updatedAt()))) {
                    continue; // its runner may be alive
                }
                // The build follows its agent: adopted only where the agent it is on is online.
                String agent = agentOf(copy.content());
                if (agent != null) {
                    hudson.model.Computer c = Jenkins.get().getComputer(agent);
                    if (c == null || c.isOffline()) {
                        continue;
                    }
                }
                File aside = new File(job.getBuildDir(), SUSPECT + number);
                byte[] state = mine && new File(aside, "build.xml").exists() && new File(aside, "program.dat").exists()
                        ? archive(aside) // its own last state on disk is at least as new as the store's copy
                        : copy.content();
                try {
                    sync.client.put(path, state, copy.version(), 0); // the claim: one replica wins it
                } catch (HomeSyncClient.Refused r) {
                    continue; // another replica claimed it, or it changed: look again next pass
                }
                sync.runningCopies.remove(path);
                adopt(job, number, state);
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

    static String agentInLog(String raw) {
        // The node's name is a hyperlink in the log file: "Running on <ESC>[8mha:...<ESC>[0magent-0 in".
        // Read as it was, it named no agent any replica had, and nothing was ever adopted.
        String log = hudson.console.ConsoleNote.removeNotes(raw);
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
