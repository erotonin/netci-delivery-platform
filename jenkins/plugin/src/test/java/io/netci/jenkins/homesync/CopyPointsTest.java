package io.netci.jenkins.homesync;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import hudson.FilePath;
import hudson.model.Result;
import java.io.File;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Set;
import java.util.function.BooleanSupplier;
import org.jenkinsci.plugins.workflow.cps.CpsFlowDefinition;
import org.jenkinsci.plugins.workflow.job.WorkflowJob;
import org.jenkinsci.plugins.workflow.job.WorkflowRun;
import org.jenkinsci.plugins.workflow.steps.GeneralNonBlockingStepExecution;
import org.jenkinsci.plugins.workflow.steps.Step;
import org.jenkinsci.plugins.workflow.steps.StepContext;
import org.jenkinsci.plugins.workflow.steps.StepDescriptor;
import org.jenkinsci.plugins.workflow.steps.StepExecution;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.RegisterExtension;
import org.jvnet.hudson.test.TestExtension;
import org.jvnet.hudson.test.junit.jupiter.JenkinsSessionExtension;
import org.kohsuke.stapler.DataBoundConstructor;

/**
 * ADR-094: a running build's copy is one it can be resumed from -- never taken while a step runs
 * background code, taken at each step boundary -- and a step that ended after the last copy fails
 * with its reason instead of being waited for.
 */
class CopyPointsTest {
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
            Thread.sleep(50);
        }
    }

    /** The log inside the store's copy of a running build, or "" when there is none. */
    private static String copiedLog(String path) {
        byte[] tar;
        synchronized (STORE) {
            FakeHomeSync.Entry e = STORE.items.get(path);
            if (e == null || e.content() == null) {
                return "";
            }
            tar = e.content();
        }
        try {
            Path dir = Files.createTempDirectory("copy");
            new FilePath(dir.toFile()).untarFrom(new java.io.ByteArrayInputStream(tar), FilePath.TarCompression.GZIP);
            Path log = dir.resolve("log");
            return Files.exists(log) ? Files.readString(log) : "";
        } catch (IOException e) {
            return "";
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            return "";
        }
    }

    /** A step that runs background code for a while, as withCredentials does while it binds. */
    public static final class Background extends Step {
        final int seconds;

        @DataBoundConstructor
        public Background(int seconds) {
            this.seconds = seconds;
        }

        @Override
        public StepExecution start(StepContext context) {
            return new Execution(context, seconds);
        }

        @TestExtension
        public static final class Descriptor extends StepDescriptor {
            @Override
            public String getFunctionName() {
                return "netciBackground";
            }

            @Override
            public Set<? extends Class<?>> getRequiredContext() {
                return Set.of();
            }
        }
    }

    static final class Execution extends GeneralNonBlockingStepExecution {
        private static final long serialVersionUID = 1L;
        private final int seconds;

        Execution(StepContext context, int seconds) {
            super(context);
            this.seconds = seconds;
        }

        @Override
        public boolean start() {
            run(() -> {
                Thread.sleep(seconds * 1000L);
                getContext().onSuccess(null);
            });
            return false;
        }
    }

    @Test
    void aBuildStoppedWhileAStepRunsBackgroundCodeIsResumedFromTheCopyBeforeIt() throws Throwable {
        STORE.reset();
        File[] builds = new File[1];
        sessions.then(j -> {
            await("the home caught up", () -> HomeSync.get() != null && HomeSync.get().ready());
            WorkflowJob job = j.createProject(WorkflowJob.class, "binding");
            job.setDefinition(new CpsFlowDefinition("echo 'before'; sleep 2; netciBackground(seconds: 6); echo 'after the binding'", true));
            WorkflowRun run = job.scheduleBuild2(0).waitForStart();
            await("a copy past 'before'", () -> copiedLog("@running/binding/1").contains("before"));
            await("in the background step", () -> RunningBuilds.blocksRestart(run));
            Thread.sleep(3_500); // a timer copy and the step's own boundary, both while it blocks
            assertTrue(run.isBuilding());
            builds[0] = job.getBuildDir();
        });
        // Not this replica's own directory: the store's copy is what another replica adopts.
        new FilePath(new File(builds[0], "1")).deleteRecursive();
        sessions.then(j -> {
            WorkflowJob job = j.jenkins.getItemByFullName("binding", WorkflowJob.class);
            await("adopted", () -> job.getBuildByNumber(1) != null);
            WorkflowRun run = job.getBuildByNumber(1);
            j.waitForCompletion(run);
            String log = run.getLog();
            assertFalse(log.contains("SynchronousResumeNotSupported"), log);
            assertEquals(Result.SUCCESS, run.getResult(), log);
            assertTrue(log.contains("after the binding"), log);
        });
    }

    @Test
    void eachStepBoundaryIsCopiedAtOnceNotOnTheTimer() throws Throwable {
        STORE.reset();
        sessions.then(j -> {
            await("the home caught up", () -> HomeSync.get() != null && HomeSync.get().ready());
            WorkflowJob job = j.createProject(WorkflowJob.class, "steps");
            job.setDefinition(new CpsFlowDefinition(
                    "for (int i = 1; i <= 4; i++) { echo \"step ${i} done\"; sleep time: 4, unit: 'SECONDS' }", true));
            WorkflowRun run = job.scheduleBuild2(0).waitForStart();
            for (int i = 1; i <= 4; i++) {
                String line = "step " + i + " done";
                await(line + " in the log", () -> {
                    try {
                        return run.getLog().contains(line);
                    } catch (IOException e) {
                        return false;
                    }
                });
                long seen = System.currentTimeMillis();
                await(line + " in the copy", () -> copiedLog("@running/steps/1").contains(line));
                long late = System.currentTimeMillis() - seen;
                // The timer alone is up to 3 s late; four boundaries all within a second are not chance.
                assertTrue(late < 1_000, line + " reached the copy " + late + " ms after the log");
            }
            j.waitForCompletion(run);
        });
    }

    @Test
    void aStepThatEndedAfterTheCopyFailsWithItsReasonInsteadOfBeingWaitedFor() throws Throwable {
        sessions.then(j -> {
            Path control = Files.createTempDirectory("durable").resolve("gone");
            Path dir = Files.createTempDirectory("copy");
            Files.writeString(dir.resolve("log"), "[Pipeline] sh\n");
            Files.writeString(dir.resolve(RunningBuilds.MARKS), "\t" + control.resolve("last-location.txt") + "\t"
                    + control.resolve("jenkins-log.txt") + "\t0\t14\n");
            RunningBuilds.putMarksBack(dir.toFile());
            assertEquals(Integer.toString(RunningBuilds.ENDED_ELSEWHERE),
                    Files.readString(control.resolve("jenkins-result.txt"), StandardCharsets.UTF_8));
            assertEquals("0", Files.readString(control.resolve("last-location.txt")));
            assertTrue(Files.readString(control.resolve("jenkins-log.txt")).contains("its result was lost"));
        });
    }
}
