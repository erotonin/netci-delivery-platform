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
        STORE.ownJobAs("r1", "taken"); // while this replica was down, r1 took the job (and its build)
        sessions.then(j -> {
            WorkflowJob job = j.jenkins.getItemByFullName("taken", WorkflowJob.class);
            await("the home caught up", () -> HomeSync.get() != null && HomeSync.get().ready());
            Thread.sleep(12_000); // two adoption passes and more
            assertNull(job.getBuildByNumber(1), "a build another live replica owns was resumed here");
            assertTrue(new File(job.getBuildDir(), RunningBuilds.SUSPECT + "1").exists(), "the unfinished copy was not set aside");
        });
    }
}
