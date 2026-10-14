package io.netci.jenkins.homesync;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.file.Files;
import java.nio.file.Path;
import org.jenkinsci.plugins.workflow.job.WorkflowJob;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.Test;
import org.jvnet.hudson.test.JenkinsRule;
import org.jvnet.hudson.test.junit.jupiter.WithJenkins;

/**
 * ADR-081: a replica that has not caught up with the store (here it never can: the store is down
 * from the start) says "retry" to netci-queue and "not ready" to the balancer -- never the 409 a
 * lone Jenkins would answer, which netci-queue takes as the run refused for good.
 */
@WithJenkins
class NotCaughtUpTest {
    static final FakeHomeSync STORE;

    static {
        try {
            STORE = new FakeHomeSync();
            STORE.down = true;
            Path token = Files.createTempFile("homesync", ".token");
            Files.writeString(token, FakeHomeSync.TOKEN);
            System.setProperty("netci.homesync.url", STORE.url());
            System.setProperty("netci.homesync.token.file", token.toString());
            System.setProperty("netci.replica.name", "me");
        } catch (IOException e) {
            throw new ExceptionInInitializerError(e);
        }
    }

    @AfterAll
    static void off() {
        System.clearProperty("netci.homesync.url");
        System.clearProperty("netci.homesync.token.file");
        System.clearProperty("netci.replica.name");
    }

    private static int call(JenkinsRule j, String path, String body) throws Exception {
        HttpRequest.Builder b = HttpRequest.newBuilder(URI.create(j.getURL() + path));
        b = body == null ? b.GET() : b.header("Content-Type", "application/json").POST(HttpRequest.BodyPublishers.ofString(body));
        return HttpClient.newHttpClient().send(b.build(), HttpResponse.BodyHandlers.discarding()).statusCode();
    }

    @Test
    void aReplicaStillCatchingUpSaysRetryAndNotReady(JenkinsRule j) throws Exception {
        j.jenkins.setCrumbIssuer(null); // an anonymous POST reaches the plugin, as netci-queue's API token does
        j.createProject(WorkflowJob.class, "waiting");
        assertFalse(HomeSync.get().ready());
        String run = "9b8d6a8e-3f2c-4c7e-9d2a-0a1b2c3d4e5f";
        assertEquals(503, call(j, "netci/dispatch", "{\"job\":\"waiting\",\"runId\":\"" + run + "\"}"));
        assertEquals(503, call(j, "netci/run?job=waiting&runId=" + run, null));
        assertEquals(503, call(j, "netci-ready/", null), "the balancer would send traffic to a replica that is not caught up");
    }
}
