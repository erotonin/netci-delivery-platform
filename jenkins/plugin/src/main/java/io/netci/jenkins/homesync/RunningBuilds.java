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

    /**
     * In a copy: where each running {@code sh} step's agent had got to in its output when the copy
     * was taken, one per line, {@code node TAB last-location file TAB output file TAB offset TAB
     * the build log's length just after the offset was read}.
     *
     * <p>When the step's output is watched (durable-task's USE_WATCHING; the lab polls, see
     * {@link LogLast}), the agent pushes it to the controller and then moves its own mark
     * ({@code last-location.txt}); a resumed step goes on from that mark. The controller writes the
     * output to the build's log before the agent moves the mark, but the copy is up to 3 s old: in
     * the soak, ticks 10 and 11 were in the dead replica's log, past the agent's mark, and in no copy
     * -- the adopted build never had them. So the marks are read before the log is copied (the copy's
     * log then holds everything up to them), and the adopter puts the agent's mark back to the copy's
     * before the build resumes -- moved on only past output the copy's log provably holds: what was
     * appended to the log after the mark was read, when it is exactly the output that follows the
     * mark. Not "whatever the log ends with": a step printing the same line again and again would
     * match more than it sent, and that output would be lost.
     */
    static final String MARKS = "netci-durable-marks";
    /** The most output compared, or sent again, when a mark is put back. */
    static final int MARK_WINDOW = 1 << 20;

    static String path(Run<?, ?> run) {
        return RUNNING + run.getParent().getFullName() + "/" + run.getNumber();
    }

    static byte[] archive(File dir) throws IOException, InterruptedException {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        try (OutputStream gz = FilePath.TarCompression.GZIP.compress(out)) {
            new FilePath(dir).tar(gz, new LogLast());
        }
        return out.toByteArray();
    }

    /**
     * A build directory with its log last. A running {@code sh} step reads its output from the agent
     * from where it got to ({@code lastLocation}, saved in program.dat after each read), writes it to
     * the log, then saves: so a log copied after program.dat holds everything up to program.dat's
     * position. Copied in name order the log came first, and a read between the two left
     * program.dat past the copy's log -- the soak's adopted build #30 never had ticks 10 and 11.
     * And flushed first: a Pipeline's log is buffered (up to seconds) before it reaches the file, so
     * the file can lag what the step has already counted as written (the test found it lacking a tick
     * the agent's mark had passed). The temporary files of an atomic write in progress are left out: one vanished mid-archive and
     * the copy failed ("closed at 0 before the 39 bytes specified in the header").
     */
    static final class LogLast extends hudson.util.DirScanner {
        private static final long serialVersionUID = 1L;

        @Override
        public void scan(File dir, hudson.util.FileVisitor visitor) throws IOException {
            new hudson.util.DirScanner.Glob("**/*", "**/*-atomic*tmp,log").scan(dir, visitor);
            File log = new File(dir, "log");
            if (log.isFile()) {
                flushLog(log);
                visitor.visit(log, "log");
            }
        }
    }

    /**
     * Push what a running build has written to its log into the file. Only a log Pipeline has open:
     * looking one up through FileLogStorage.forFile would open storage for a directory set aside or
     * about to be replaced. Through reflection: workflow-api is not a dependency of this plugin.
     */
    static void flushLog(File log) {
        try {
            Class<?> c = Jenkins.get().getPluginManager().uberClassLoader.loadClass("org.jenkinsci.plugins.workflow.log.FileLogStorage");
            java.lang.reflect.Field open = c.getDeclaredField("openStorages");
            open.setAccessible(true);
            Object storage;
            synchronized (c) { // forFile is static synchronized: the map's lock
                storage = ((Map<?, ?>) open.get(null)).get(log);
            }
            if (storage == null) {
                return;
            }
            java.lang.reflect.Field bos = c.getDeclaredField("bos");
            bos.setAccessible(true);
            Object out = bos.get(storage);
            if (out instanceof OutputStream o) {
                o.flush(); // BufferedOutputStream's flush is synchronized with its writes
            }
        } catch (ReflectiveOperationException | IOException | RuntimeException e) {
            LOGGER.log(Level.WARNING, "netCI homesync: cannot flush " + log + " before copying it; the copy may lack its last seconds", e);
        }
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
                writeMarks(run);
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

    /** The marks of this run's running {@code sh} steps, written into its directory before it is copied. */
    static void writeMarks(Run<?, ?> run) throws IOException, InterruptedException {
        StringBuilder out = new StringBuilder();
        java.util.List<Object> executions = new java.util.ArrayList<>();
        try {
            org.jenkinsci.plugins.workflow.steps.StepExecution.applyAll(org.jenkinsci.plugins.workflow.steps.StepExecution.class, e -> {
                if (e.getClass().getName().equals("org.jenkinsci.plugins.workflow.steps.durable_task.DurableTaskStep$Execution")) {
                    try {
                        if (run.equals(e.getContext().get(Run.class))) {
                            executions.add(e);
                        }
                    } catch (IOException | InterruptedException ex) {
                        // not this run's, or gone
                    }
                }
                return null;
            }).get(5, TimeUnit.SECONDS);
        } catch (java.util.concurrent.ExecutionException | java.util.concurrent.TimeoutException e) {
            throw new IOException("cannot list the running steps of " + run, e);
        }
        for (Object e : executions) {
            try {
                Object controller = field(e, "controller");
                String node = (String) field(e, "node");
                String remote = (String) field(e, "remote");
                if (controller == null || remote == null || !watchesAFile(controller.getClass())) {
                    continue; // not a step whose output the agent watches into a file
                }
                hudson.model.Computer computer = Jenkins.get().getComputer(node == null ? "" : node);
                hudson.remoting.VirtualChannel channel = computer == null ? null : computer.getChannel();
                if (channel == null) {
                    continue;
                }
                FilePath ws = new FilePath(channel, remote);
                FilePath mark = (FilePath) call(controller, "getLastLocationFile", ws);
                long at = mark.exists() ? Long.parseLong(mark.readToString().trim()) : 0;
                File logFile = new File(run.getRootDir(), "log");
                flushLog(logFile);
                long logged = logFile.length(); // holds all the output up to `at`
                out.append(node == null ? "" : node).append('\t').append(mark.getRemote()).append('\t')
                        .append(((FilePath) call(controller, "getLogFile", ws)).getRemote()).append('\t').append(at).append('\t').append(logged).append('\n');
            } catch (ReflectiveOperationException | NumberFormatException ex) {
                LOGGER.log(Level.WARNING, "netCI homesync: cannot read where a step of " + run + " got to in its output", ex);
            }
        }
        Files.writeString(new File(run.getRootDir(), MARKS).toPath(), out.toString(), StandardCharsets.UTF_8);
    }

    /** A durable-task controller that keeps the step's output in a file (sh's is a subclass of it). */
    private static boolean watchesAFile(Class<?> c) {
        for (; c != null; c = c.getSuperclass()) {
            if (c.getName().equals("org.jenkinsci.plugins.durabletask.FileMonitoringTask$FileMonitoringController")) {
                return true;
            }
        }
        return false;
    }

    /** A public method of durable-task's controller, whose class is a protected nested one. */
    private static Object call(Object o, String name, FilePath ws) throws ReflectiveOperationException {
        for (Class<?> c = o.getClass(); c != null; c = c.getSuperclass()) {
            try {
                java.lang.reflect.Method m = c.getDeclaredMethod(name, FilePath.class);
                m.setAccessible(true);
                return m.invoke(o, ws);
            } catch (NoSuchMethodException e) {
                // declared higher up
            }
        }
        throw new NoSuchMethodException(name);
    }

    private static Object field(Object o, String name) throws ReflectiveOperationException {
        java.lang.reflect.Field f = o.getClass().getDeclaredField(name);
        f.setAccessible(true);
        return f.get(o);
    }

    /**
     * Put each agent's mark back to the copy's, before the build resumes: output past the copy's
     * mark is sent again, except what was appended to the copy's log after the mark was read when
     * that is exactly the output following the mark. Never forward past what the log holds, so
     * nothing is lost; output is shown twice only when the appended part is not just this step's
     * output (another branch wrote too, or a chunk was in flight when the mark was read).
     */
    static void putMarksBack(File dir) throws IOException, InterruptedException {
        File marks = new File(dir, MARKS);
        if (!marks.exists()) {
            return;
        }
        byte[] log = Files.readAllBytes(new File(dir, "log").toPath());
        for (String line : Files.readAllLines(marks.toPath(), StandardCharsets.UTF_8)) {
            String[] f = line.split("\t");
            if (f.length != 5) {
                continue;
            }
            hudson.model.Computer computer = Jenkins.get().getComputer(f[0]);
            hudson.remoting.VirtualChannel channel = computer == null ? null : computer.getChannel();
            if (channel == null) {
                LOGGER.warning(() -> "netCI homesync: " + f[0] + " is not connected here; its step's output resumes from the agent's own mark");
                continue;
            }
            FilePath mark = new FilePath(channel, f[1]);
            long copied = Long.parseLong(f[3]);
            long now = mark.exists() ? Long.parseLong(mark.readToString().trim()) : 0;
            if (now <= copied) {
                continue;
            }
            byte[] since;
            try (java.io.InputStream in = new FilePath(channel, f[2]).readFromOffset(copied)) {
                since = in.readNBytes((int) Math.min(now - copied, MARK_WINDOW));
            }
            int held = held(log, Integer.parseInt(f[4]), since);
            long back = copied + held;
            mark.write(Long.toString(back), StandardCharsets.UTF_8.name());
            LOGGER.info(() -> "netCI homesync: " + f[0] + "'s step output put back from " + now + " to " + back
                    + " (the copy's log held " + held + " bytes past its mark); " + (now - back) + " bytes are sent again");
        }
    }

    /**
     * How many bytes of the output past the mark the copy's log holds: everything appended to the log
     * after the mark was read ({@code log[logged:]}) when it is exactly the start of that output,
     * else none.
     */
    static int held(byte[] log, int logged, byte[] since) {
        if (logged < 0 || logged > log.length) {
            return 0;
        }
        int appended = log.length - logged;
        if (appended > since.length) {
            return 0;
        }
        return java.util.Arrays.equals(log, logged, log.length, since, 0, appended) ? appended : 0;
    }

    static void adopt(Job<?, ?> job, int number, byte[] copy) throws IOException, InterruptedException {
        File dir = new File(job.getBuildDir(), Integer.toString(number));
        new FilePath(new File(job.getBuildDir(), SUSPECT + number)).deleteRecursive(); // the stale local copy, if any
        new FilePath(dir).deleteRecursive();
        if (!dir.mkdirs()) {
            throw new IOException("cannot create " + dir);
        }
        new FilePath(dir).untarFrom(new ByteArrayInputStream(copy), FilePath.TarCompression.GZIP);
        putMarksBack(dir); // before the run is loaded: loading it resumes its steps from the agents' marks
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
