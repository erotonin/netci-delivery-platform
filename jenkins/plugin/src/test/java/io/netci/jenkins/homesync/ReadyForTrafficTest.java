package io.netci.jenkins.homesync;

import static org.junit.jupiter.api.Assertions.assertEquals;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.file.Files;
import java.nio.file.Path;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.Test;
import org.jvnet.hudson.test.JenkinsRule;
import org.jvnet.hudson.test.junit.jupiter.WithJenkins;

/** ADR-080: the balancer's check answers without credentials, and 503 once the replica drains. */
@WithJenkins
class ReadyForTrafficTest {
    static final Path DRAIN;

    static {
        try {
            DRAIN = Files.createTempDirectory("drain").resolve("netci-draining");
        } catch (java.io.IOException e) {
            throw new ExceptionInInitializerError(e);
        }
        System.setProperty("netci.drain.file", DRAIN.toString());
    }

    @AfterAll
    static void off() {
        System.clearProperty("netci.drain.file");
    }

    private static int status(JenkinsRule j) throws Exception {
        // No credentials, as the balancer asks.
        HttpRequest req = HttpRequest.newBuilder(URI.create(j.getURL() + "netci-ready/")).GET().build();
        return HttpClient.newHttpClient().send(req, HttpResponse.BodyHandlers.discarding()).statusCode();
    }

    @Test
    void readyUntilTheReplicaDrains(JenkinsRule j) throws Exception {
        j.jenkins.setSecurityRealm(j.createDummySecurityRealm());
        j.jenkins.setAuthorizationStrategy(new org.jvnet.hudson.test.MockAuthorizationStrategy()); // nobody may read anything
        assertEquals(200, status(j));
        Files.writeString(DRAIN, "");
        assertEquals(503, status(j), "a draining replica still looked ready");
        Files.delete(DRAIN);
        assertEquals(200, status(j));
    }
}
