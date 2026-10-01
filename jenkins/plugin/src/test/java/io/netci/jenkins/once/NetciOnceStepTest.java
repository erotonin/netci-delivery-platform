package io.netci.jenkins.once;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.sun.net.httpserver.HttpServer;
import hudson.model.Result;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.atomic.AtomicInteger;
import net.sf.json.JSONObject;
import org.jenkinsci.plugins.workflow.cps.CpsFlowDefinition;
import org.jenkinsci.plugins.workflow.job.WorkflowJob;
import org.jenkinsci.plugins.workflow.job.WorkflowRun;
import org.jenkinsci.plugins.workflow.test.steps.SemaphoreStep;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.RegisterExtension;
import org.junit.jupiter.api.io.TempDir;
import org.jvnet.hudson.test.JenkinsRule;
import org.jvnet.hudson.test.junit.jupiter.JenkinsSessionExtension;

class NetciOnceStepTest {
    @RegisterExtension
    private final JenkinsSessionExtension sessions = new JenkinsSessionExtension();

    @TempDir
    static Path tmp;

    /** netci-queue's /v1/once: first record or same nonce -> 200, another nonce -> 409. */
    static HttpServer queue;
    static final Map<String, String> markers = new ConcurrentHashMap<>();
    static final List<JSONObject> calls = new CopyOnWriteArrayList<>();
    static final AtomicInteger unavailable = new AtomicInteger();

    @BeforeAll
    static void startQueue() throws Exception {
        queue = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        queue.createContext("/v1/once", ex -> {
            if (!"Bearer cell-b-token".equals(ex.getRequestHeaders().getFirst("Authorization"))) {
                ex.sendResponseHeaders(401, -1);
                ex.close();
                return;
            }
            JSONObject req = JSONObject.fromObject(new String(ex.getRequestBody().readAllBytes(), StandardCharsets.UTF_8));
            calls.add(req);
            int status;
            String body;
            if (unavailable.getAndDecrement() > 0) {
                status = 503;
                body = "{}";
            } else {
                String id = req.getString("scope") + "|" + req.getString("key");
                String first = markers.putIfAbsent(id, req.getString("nonce"));
                boolean ok = first == null || first.equals(req.getString("nonce"));
                status = ok ? 200 : 409;
                body = new JSONObject().element("first", ok).element("recordedAt", "2026-10-01T10:00:00Z").toString();
            }
            byte[] b = body.getBytes(StandardCharsets.UTF_8);
            ex.sendResponseHeaders(status, b.length);
            ex.getResponseBody().write(b);
            ex.close();
        });
        queue.start();
    }

    @AfterAll
    static void stopQueue() {
        queue.stop(0);
    }

    @BeforeEach
    void reset() {
        markers.clear();
        calls.clear();
        unavailable.set(0);
    }

    private static void configure(JenkinsRule r, String url) throws Exception {
        Path token = tmp.resolve("token");
        Files.writeString(token, "cell-b-token\n");
        NetciConfig c = NetciConfig.get();
        c.setQueueUrl(url);
        c.setTokenFile(token.toString());
        c.setRetrySeconds(4);
    }

    private static String url() {
        return "http://127.0.0.1:" + queue.getAddress().getPort();
    }

    private static WorkflowJob job(JenkinsRule r, String script) throws Exception {
        WorkflowJob p = r.jenkins.createProject(WorkflowJob.class, "deploy");
        p.setDefinition(new CpsFlowDefinition(script, true));
        return p;
    }

    @Test
    void theBodyRunsOnceAndIsRecordedForThisBuild() throws Throwable {
        sessions.then(r -> {
            configure(r, url());
            WorkflowRun b = r.buildAndAssertSuccess(job(r, "netciOnce('deploy-prod') { echo 'deploying v42' }"));
            r.assertLogContains("deploying v42", b);
            assertEquals(1, calls.size());
            JSONObject c = calls.get(0);
            assertEquals("deploy-prod", c.getString("key"));
            assertTrue(c.getString("scope").endsWith("/deploy#1"), c.getString("scope"));
            assertTrue(c.getString("nonce").length() >= 16);
        });
    }

    @Test
    void aBlockStartedAgainUnderAnotherAttemptIsRefused() throws Throwable {
        // Two starts of the same block in one build: what a rollback after a takeover looks like.
        sessions.then(r -> {
            configure(r, url());
            WorkflowJob p = job(r, "netciOnce('deploy-prod') { echo 'first' }\nnetciOnce('deploy-prod') { echo 'second' }");
            WorkflowRun b = r.buildAndAssertStatus(Result.FAILURE, p);
            r.assertLogContains("first", b);
            r.assertLogNotContains("second", b);
            r.assertLogContains("already started in this build", b);
            r.assertLogContains("retry: true", b);
        });
    }

    @Test
    void aPersonCanRunItAgainOnPurpose() throws Throwable {
        sessions.then(r -> {
            configure(r, url());
            WorkflowRun b = r.buildAndAssertSuccess(job(r,
                    "netciOnce('deploy-prod') { echo 'first' }\nnetciOnce(key: 'deploy-prod', retry: true) { echo 'again, on purpose' }"));
            r.assertLogContains("again, on purpose", b);
        });
    }

    @Test
    void whatCannotBeCheckedIsNotRun() throws Throwable {
        sessions.then(r -> {
            configure(r, "http://127.0.0.1:1"); // nothing listens there
            WorkflowRun b = r.buildAndAssertStatus(Result.FAILURE, job(r, "netciOnce('deploy-prod') { echo 'deploying' }"));
            r.assertLogNotContains("deploying", b);
            r.assertLogContains("cannot tell whether this block ran before", b);
        });
    }

    @Test
    void aBriefOutageIsWaitedOut() throws Throwable {
        sessions.then(r -> {
            configure(r, url());
            unavailable.set(1);
            r.buildAndAssertSuccess(job(r, "netciOnce('deploy-prod') { echo 'deploying' }"));
            assertEquals(2, calls.size());
        });
    }

    @Test
    void anUnconfiguredGuardDoesNotRunTheBlock() throws Throwable {
        sessions.then(r -> {
            WorkflowRun b = r.buildAndAssertStatus(Result.FAILURE, job(r, "netciOnce('deploy-prod') { echo 'deploying' }"));
            r.assertLogNotContains("deploying", b);
        });
    }

    @Test
    void aControllerRestartInsideTheBlockResumesItWithoutAskingAgain() throws Throwable {
        sessions.then(r -> {
            configure(r, url());
            WorkflowJob p = job(r, "netciOnce('deploy-prod') { semaphore 'inside'; echo 'finished the deployment' }");
            WorkflowRun b = p.scheduleBuild2(0).waitForStart();
            SemaphoreStep.waitForStart("inside/1", b);
        });
        sessions.then(r -> {
            WorkflowRun b = r.jenkins.getItemByFullName("deploy", WorkflowJob.class).getBuildByNumber(1);
            SemaphoreStep.success("inside/1", null);
            r.assertBuildStatusSuccess(r.waitForCompletion(b));
            r.assertLogContains("finished the deployment", b);
            assertEquals(1, calls.size(), "a resumed block asked again");
        });
    }
}
