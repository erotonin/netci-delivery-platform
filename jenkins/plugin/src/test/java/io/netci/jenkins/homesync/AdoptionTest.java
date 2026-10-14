package io.netci.jenkins.homesync;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

import hudson.model.Result;
import java.io.File;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.function.BooleanSupplier;
import org.jenkinsci.plugins.workflow.cps.CpsFlowDefinition;
import org.jenkinsci.plugins.workflow.job.WorkflowJob;
import org.jenkinsci.plugins.workflow.job.WorkflowRun;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.RegisterExtension;
import org.jvnet.hudson.test.junit.jupiter.JenkinsSessionExtension;

/** ADR-079: a build running when its replica stopped is resumed once, in one place. */
class AdoptionTest {
    static final FakeHomeSync STORE;

    static {
        try {
            STORE = new FakeHomeSync();
            Path token = Files.createTempFile("homesync", ".token");
            Files.writeString(token, FakeHomeSync.TOKEN);
            System.setProperty("netci.homesync.url", STORE.url());
            System.setProperty("netci.homesync.token.file", token.toString());
            System.setProperty("netci.replica.name", "me");
        } catch (IOException e) {
            throw new ExceptionInInitializerError(e);
        }
    }

    @RegisterExtension
    private final JenkinsSessionExtension sessions = new JenkinsSessionExtension();

    @AfterAll
    static void off() {
        System.clearProperty("netci.homesync.url");
        System.clearProperty("netci.homesync.token.file");
        System.clearProperty("netci.replica.name");
    }

    private static void await(String what, BooleanSupplier ok) throws InterruptedException {
        long end = System.currentTimeMillis() + 60_000;
        while (!ok.getAsBoolean()) {
            if (System.currentTimeMillis() > end) {
                throw new AssertionError("timed out waiting: " + what);
            }
            Thread.sleep(100);
        }
    }

    private static boolean stored(String path) {
        synchronized (STORE) {
            FakeHomeSync.Entry e = STORE.items.get(path);
            return e != null && e.content() != null;
        }
    }

    /** The copy in the store holds these entries (it is a gzipped tar). */
    private static boolean copyHas(String path, String... names) {
        byte[] tar;
        synchronized (STORE) {
            FakeHomeSync.Entry e = STORE.items.get(path);
            if (e == null || e.content() == null) {
                return false;
            }
            tar = e.content();
        }
        try {
            Path dir = Files.createTempDirectory("copy");
            new hudson.FilePath(dir.toFile()).untarFrom(new java.io.ByteArrayInputStream(tar), hudson.FilePath.TarCompression.GZIP);
            for (String name : names) {
                if (!Files.exists(dir.resolve(name))) {
                    return false;
                }
            }
            return true;
        } catch (IOException e) {
            return false;
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            return false;
        }
    }

    private static void startLong(org.jvnet.hudson.test.JenkinsRule j, String name) throws Exception {
        await("the home caught up", () -> HomeSync.get() != null && HomeSync.get().ready());
        WorkflowJob job = j.createProject(WorkflowJob.class, name);
        job.setDefinition(new CpsFlowDefinition("sleep time: 15, unit: 'SECONDS'; echo 'after the sleep'", true));
        WorkflowRun run = job.scheduleBuild2(0).waitForStart();
        await("a running copy that can be adopted", () -> copyHas("@running/" + name + "/1", "build.xml", "program.dat"));
        assertTrue(run.isBuilding());
    }

    @Test
    void aBuildRunningWhenThisReplicaStoppedIsResumedOnceThroughAdoption() throws Throwable {
        STORE.reset();
        sessions.then(j -> startLong(j, "long"));
        sessions.then(j -> {
            WorkflowJob job = j.jenkins.getItemByFullName("long", WorkflowJob.class);
            await("adopted", () -> job.getBuildByNumber(1) != null);
            WorkflowRun run = job.getBuildByNumber(1);
            j.waitForCompletion(run);
            assertEquals(Result.SUCCESS, run.getResult());
            String log = run.getLog();
            assertTrue(log.contains("after the sleep"), log);
            assertEquals(1, log.split("Resuming build", -1).length - 1, "resumed more than once:\n" + log);
            await("the running copy removed", () -> !stored("@running/long/1"));
            await("the finished build published", () -> stored("@builds/long/1"));
            assertTrue(!new File(job.getBuildDir(), RunningBuilds.SUSPECT + "1").exists(), "the set-aside copy was left behind");
        });
    }

    @Test
    void aBuildAnotherLiveReplicaTookIsNotResumedHere() throws Throwable {
        STORE.reset();
        sessions.then(j -> startLong(j, "taken"));
        // While this replica was down, r1 adopted the build: its copy is now r1's (the claim), and r1
        // is alive -- there is no fence here to say otherwise. The job's lease is not what decides:
        // it can move for a new build while an orphan waits (the lab's #7 and #8).
        synchronized (STORE) {
            STORE.changeFrom("r1", "@running/taken/1", STORE.items.get("@running/taken/1").content());
        }
        sessions.then(j -> {
            WorkflowJob job = j.jenkins.getItemByFullName("taken", WorkflowJob.class);
            await("the home caught up", () -> HomeSync.get() != null && HomeSync.get().ready());
            Thread.sleep(12_000); // two adoption passes and more
            assertNull(job.getBuildByNumber(1), "a build another live replica adopted was resumed here too");
            assertTrue(new File(job.getBuildDir(), RunningBuilds.SUSPECT + "1").exists(), "the unfinished copy was not set aside");
        });
    }

    private static byte[] bytes(String s) {
        return s.getBytes(java.nio.charset.StandardCharsets.UTF_8);
    }

    private static String ticks(int from, int to) {
        StringBuilder b = new StringBuilder();
        for (int i = from; i <= to; i++) {
            b.append("tick ").append(i).append('\n');
        }
        return b.toString();
    }

    /**
     * Only what was appended after the mark was read, and only when it is exactly the output after
     * the mark. A step repeating one line: the log ends with that line, the output after the mark
     * starts with it -- "whatever the log ends with" would skip output the log never got.
     */
    @Test
    void theCopysLogHoldsOnlyWhatWasProvablyAppendedAfterTheMark() {
        byte[] log = bytes("[Pipeline] sh\n" + ticks(1, 9));
        int logged = bytes("[Pipeline] sh\n" + ticks(1, 8)).length;
        assertEquals(bytes("tick 9\n").length, RunningBuilds.held(log, logged, bytes(ticks(9, 12))));
        assertEquals(0, RunningBuilds.held(log, logged, bytes(ticks(10, 12))), "appended output that is not what follows the mark");
        byte[] other = bytes("[Pipeline] sh\n" + ticks(1, 8) + "[other] line\n");
        assertEquals(0, RunningBuilds.held(other, logged, bytes(ticks(9, 12))), "another branch's output counted as this step's");
        byte[] same = bytes("x\nx\nx\nx\n");
        assertEquals(0, RunningBuilds.held(same, same.length, bytes("x\nx\n")), "nothing was appended after the mark, yet some output was skipped");
    }

    /** The agent's mark put back to the copy's, plus what the copy's log holds past it -- never forward. */
    @Test
    void anAdoptedStepsOutputResumesFromWhatTheCopysLogHolds() throws Throwable {
        sessions.then(j -> {
            Path agent = Files.createTempDirectory("durable");
            Files.writeString(agent.resolve("jenkins-log.txt"), ticks(1, 12));
            Path mark = agent.resolve("last-location.txt");
            Files.writeString(mark, Long.toString(bytes(ticks(1, 12)).length)); // the dead replica got up to 12
            Path dir = Files.createTempDirectory("copy");
            String head = "[Pipeline] sh\n";
            Files.writeString(dir.resolve("log"), head + ticks(1, 9)); // the copy: 9 arrived just after the mark was read
            Files.writeString(dir.resolve(RunningBuilds.MARKS), "\t" + mark + "\t" + agent.resolve("jenkins-log.txt") + "\t"
                    + bytes(ticks(1, 8)).length + "\t" + bytes(head + ticks(1, 8)).length + "\n");
            RunningBuilds.putMarksBack(dir.toFile());
            assertEquals(Long.toString(bytes(ticks(1, 9)).length), Files.readString(mark), "10, 11 and 12 are not sent again exactly once");

            Files.writeString(mark, "3"); // the agent's mark is behind the copy's: left alone
            RunningBuilds.putMarksBack(dir.toFile());
            assertEquals("3", Files.readString(mark));
        });
    }

    /** The log goes last into a copy, after program.dat, which says how far a running sh step had read. */
    @Test
    void aCopyHoldsTheLogLast() throws Exception {
        Path dir = Files.createTempDirectory("build");
        for (String name : new String[] {"build.xml", "log", "log-index", "program.dat"}) {
            Files.writeString(dir.resolve(name), name);
        }
        Files.createDirectories(dir.resolve("workflow"));
        Files.writeString(dir.resolve("workflow/2.xml"), "node");
        Files.writeString(dir.resolve("build.xml-atomic123tmp"), "half-written");
        java.util.List<String> names = new java.util.ArrayList<>();
        byte[] tar;
        try (java.io.InputStream gz = new java.util.zip.GZIPInputStream(new java.io.ByteArrayInputStream(RunningBuilds.archive(dir.toFile())))) {
            tar = gz.readAllBytes();
        }
        // The tar format read by hand, in order: a 512-byte header (name at 0, octal size at 124,
        // type at 156), then the content padded to 512.
        for (int at = 0; at + 512 <= tar.length && tar[at] != 0; ) {
            String name = new String(tar, at, 100, java.nio.charset.StandardCharsets.US_ASCII).replaceAll("\u0000.*", "");
            long size = Long.parseLong(new String(tar, at + 124, 12, java.nio.charset.StandardCharsets.US_ASCII).replaceAll("[^0-7]", "").isEmpty()
                    ? "0" : new String(tar, at + 124, 12, java.nio.charset.StandardCharsets.US_ASCII).replaceAll("[^0-7]", ""), 8);
            if (tar[at + 156] != '5') {
                names.add(name);
            }
            at += 512 + (int) ((size + 511) / 512 * 512);
        }
        assertEquals("log", names.get(names.size() - 1), names.toString());
        assertTrue(names.containsAll(java.util.List.of("build.xml", "log-index", "program.dat", "workflow/2.xml")), names.toString());
        assertTrue(names.stream().noneMatch(n -> n.contains("atomic")), names.toString());
    }

    /** A running sh step's copy carries its mark, and the copy's log holds all the output up to it. */
    @Test
    void aCopyCarriesEachRunningStepsMarkWithTheOutputUpToIt() throws Throwable {
        STORE.reset();
        sessions.then(j -> {
            await("the home caught up", () -> HomeSync.get() != null && HomeSync.get().ready());
            // The marks are the agent's when it watches the output; the test JVM polls by default.
            java.lang.reflect.Field watching = j.jenkins.getPluginManager().uberClassLoader
                    .loadClass("org.jenkinsci.plugins.workflow.steps.durable_task.DurableTaskStep").getField("USE_WATCHING");
            boolean was = watching.getBoolean(null);
            watching.setBoolean(null, true);
            try {
            WorkflowJob job = j.createProject(WorkflowJob.class, "ticking");
            job.setDefinition(new CpsFlowDefinition("node { sh 'for i in $(seq 1 150); do echo tick $i; sleep 0.2; done' }", true));
            WorkflowRun run = job.scheduleBuild2(0).waitForStart();
            String[] marked = new String[1];
            await("a copy with a mark past the first ticks", () -> {
                byte[] tar;
                synchronized (STORE) {
                    FakeHomeSync.Entry e = STORE.items.get("@running/ticking/1");
                    tar = e == null ? null : e.content();
                }
                if (tar == null) {
                    return false;
                }
                try {
                    Path dir = Files.createTempDirectory("copy");
                    new hudson.FilePath(dir.toFile()).untarFrom(new java.io.ByteArrayInputStream(tar), hudson.FilePath.TarCompression.GZIP);
                    Path marks = dir.resolve(RunningBuilds.MARKS);
                    if (!Files.exists(marks)) {
                        return false;
                    }
                    for (String line : Files.readAllLines(marks)) {
                        String[] f = line.split("\t");
                        if (f.length == 5 && Long.parseLong(f[3]) > 20) {
                            byte[] out = Files.readAllBytes(Path.of(f[2]));
                            String upToMark = new String(out, 0, (int) Long.parseLong(f[3]), java.nio.charset.StandardCharsets.UTF_8);
                            marked[0] = upToMark + "\u0000" + Files.readString(dir.resolve("log"));
                            return true;
                        }
                    }
                    return false;
                } catch (IOException e) {
                    return false;
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                    return false;
                }
            });
            String[] parts = marked[0].split("\u0000", 2);
            for (String tick : parts[0].split("\n")) {
                if (tick.startsWith("tick ")) {
                    assertTrue(parts[1].contains(tick + "\n"), "the copy's log lacks " + tick + ", which the agent's mark had passed");
                }
            }
            run.doStop();
            j.waitForCompletion(run);
            } finally {
                watching.setBoolean(null, was);
            }
        });
    }
}
