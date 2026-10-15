package io.netci.jenkins.homesync;

import static org.junit.jupiter.api.Assertions.assertEquals;

import hudson.model.UnprotectedRootAction;
import java.io.IOException;
import java.net.ServerSocket;
import java.net.Socket;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.Test;
import org.jvnet.hudson.test.JenkinsRule;
import org.jvnet.hudson.test.TestExtension;
import org.jvnet.hudson.test.junit.jupiter.WithJenkins;
import org.kohsuke.stapler.HttpResponses;

/** ADR-089: a drain is told to the balancer by the agent check, and the requests in flight are counted. */
@WithJenkins
class DrainAgentTest {
    static final Path DRAIN;
    static final int PORT;
    static final CountDownLatch SLOW_STARTED = new CountDownLatch(1);
    static final CountDownLatch SLOW_GO = new CountDownLatch(1);

    static {
        try (ServerSocket free = new ServerSocket(0)) {
            DRAIN = Files.createTempDirectory("drain").resolve("netci-draining");
            PORT = free.getLocalPort();
        } catch (IOException e) {
            throw new ExceptionInInitializerError(e);
        }
        System.setProperty("netci.drain.file", DRAIN.toString());
        System.setProperty("netci.drain.agent.port", Integer.toString(PORT));
    }

    @AfterAll
    static void off() {
        System.clearProperty("netci.drain.file");
        System.clearProperty("netci.drain.agent.port");
    }

    /** A page that is slow to render, like a dashboard of 22,000 jobs. */
    @TestExtension
    public static final class Slow implements UnprotectedRootAction {
        @Override
        public String getUrlName() {
            return "slow-page";
        }

        @Override
        public String getIconFileName() {
            return null;
        }

        @Override
        public String getDisplayName() {
            return null;
        }

        public org.kohsuke.stapler.HttpResponse doIndex() throws InterruptedException {
            SLOW_STARTED.countDown();
            SLOW_GO.await(30, TimeUnit.SECONDS);
            return HttpResponses.text("rendered");
        }
    }

    private static String agent() throws IOException {
        try (Socket s = new Socket("127.0.0.1", PORT)) {
            s.setSoTimeout(2000);
            return new String(s.getInputStream().readAllBytes(), StandardCharsets.US_ASCII);
        }
    }

    private static HttpResponse<String> get(JenkinsRule j, String path) throws Exception {
        return HttpClient.newHttpClient().send(HttpRequest.newBuilder(URI.create(j.getURL() + path)).GET().build(),
                HttpResponse.BodyHandlers.ofString());
    }

    @Test
    void aDrainIsMaintenanceToTheBalancerAndWhatIsInFlightFinishes(JenkinsRule j) throws Exception {
        assertEquals("ready\n", agent());
        CompletableFuture<HttpResponse<String>> slow = CompletableFuture.supplyAsync(() -> {
            try {
                return get(j, "slow-page/");
            } catch (Exception e) {
                throw new IllegalStateException(e);
            }
        });
        SLOW_STARTED.await(30, TimeUnit.SECONDS);
        assertEquals("1", get(j, "netci-ready/busy").body(), "the slow page is not counted in flight");

        Files.writeString(DRAIN, "");
        assertEquals("maint\n", agent());
        // The health check stays up: a 503 would have the balancer close the slow page's connection.
        assertEquals(200, get(j, "netci-ready/").statusCode());

        SLOW_GO.countDown();
        assertEquals("rendered", slow.get(30, TimeUnit.SECONDS).body());
        assertEquals("0", get(j, "netci-ready/busy").body());

        Files.delete(DRAIN);
        assertEquals("ready\n", agent());
    }
}
