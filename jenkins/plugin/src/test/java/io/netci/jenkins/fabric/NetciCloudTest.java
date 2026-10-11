package io.netci.jenkins.fabric;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import hudson.model.FreeStyleBuild;
import hudson.model.FreeStyleProject;
import hudson.model.Label;
import hudson.model.Node;
import java.io.File;
import java.io.IOException;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.atomic.AtomicInteger;
import jenkins.model.JenkinsLocationConfiguration;
import net.sf.json.JSONObject;
import org.jenkinsci.plugins.workflow.cps.CpsFlowDefinition;
import org.jenkinsci.plugins.workflow.job.WorkflowJob;
import org.jenkinsci.plugins.workflow.job.WorkflowRun;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import org.jvnet.hudson.test.JenkinsRule;
import org.jvnet.hudson.test.junit.jupiter.WithJenkins;

@WithJenkins
class NetciCloudTest {
    @TempDir
    Path tmp;

    private JenkinsRule r;
    private HttpServer fabric;
    private final List<JSONObject> claims = new CopyOnWriteArrayList<>();
    private final List<String> releases = new CopyOnWriteArrayList<>();
    private final Map<String, Process> agents = new ConcurrentHashMap<>();
    private final AtomicInteger refuse = new AtomicInteger();
    private final AtomicInteger granted = new AtomicInteger();
    /** How many of the next granted claims get no agent and are reported failed, as the fabric does for a broken sandbox. */
    private final AtomicInteger dead = new AtomicInteger();
    private final Map<String, String> states = new ConcurrentHashMap<>();
    /** How many of the next granted claims take their binding but never connect an agent (a sandbox cut off from the controller). */
    private final AtomicInteger silent = new AtomicInteger();

    /** A fabric whose sandboxes are agent processes started on claim, as netci-sandbox would. */
    @BeforeEach
    void setUp(JenkinsRule r) throws Exception {
        this.r = r;
        JenkinsLocationConfiguration.get().setUrl(r.getURL().toString());
        Path token = tmp.resolve("token");
        Files.writeString(token, "cell-b-token\n");
        fabric = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        fabric.createContext("/v1/claims", this::serve);
        fabric.start();
        NetciCloud cloud = new NetciCloud("fabric");
        cloud.setFabricUrl("http://127.0.0.1:" + fabric.getAddress().getPort());
        cloud.setTokenFile(token.toString());
        cloud.setConnectTimeoutSeconds(120);
        NetciCloud.NetciPool pool = new NetciCloud.NetciPool("standard", "netci-linux linux");
        pool.setRemoteFs(tmp.resolve("workspace").toString());
        cloud.setPools(List.of(pool));
        r.jenkins.clouds.add(cloud);
    }

    @AfterEach
    void tearDown() {
        agents.values().forEach(Process::destroyForcibly);
        fabric.stop(0);
    }

    private void serve(HttpExchange ex) throws IOException {
        if (!"Bearer cell-b-token".equals(ex.getRequestHeaders().getFirst("Authorization"))) {
            reply(ex, 401, "{}");
            return;
        }
        if ("GET".equals(ex.getRequestMethod())) {
            String id = ex.getRequestURI().getPath().substring("/v1/claims/".length());
            String state = states.get(id);
            reply(ex, state == null ? 404 : 200, state == null ? "{\"detail\":\"no such claim\"}"
                    : new JSONObject().element("id", id).element("state", state).element("reason", "a warm sandbox did not take its binding").toString());
            return;
        }
        if ("DELETE".equals(ex.getRequestMethod())) {
            String id = ex.getRequestURI().getPath().substring("/v1/claims/".length());
            releases.add(id);
            states.put(id, "released");
            Process p = agents.remove(id);
            if (p != null) {
                p.destroy();
            }
            reply(ex, 204, "");
            return;
        }
        JSONObject c = JSONObject.fromObject(new String(ex.getRequestBody().readAllBytes(), StandardCharsets.UTF_8));
        claims.add(c);
        if (refuse.getAndDecrement() > 0) {
            reply(ex, 429, "{\"detail\":\"the pool is at its maximum\"}");
            return;
        }
        String id = UUID.randomUUID().toString();
        granted.incrementAndGet();
        if (dead.getAndDecrement() > 0) {
            states.put(id, "failed"); // granted, and then given up: no agent will ever connect
        } else if (silent.getAndDecrement() > 0) {
            states.put(id, "bound"); // the binding taken, and no agent ever arrives
        } else {
            states.put(id, "claimed");
            agents.put(id, startAgent(c.getString("controller"), c.getString("agent"), c.getString("secret")));
        }
        reply(ex, 200, new JSONObject().element("id", id).element("pod", "sbx-" + id.substring(0, 8)).element("warm", true).toString());
    }

    private Process startAgent(String controller, String name, String secret) throws IOException {
        File remoting = new File(hudson.remoting.Launcher.class.getProtectionDomain().getCodeSource().getLocation().getPath());
        Path work = Files.createDirectories(tmp.resolve(name));
        Path secretFile = work.resolve("secret");
        Files.writeString(secretFile, secret);
        // The same arguments netci-sandbox gives agent.jar.
        return new ProcessBuilder(System.getProperty("java.home") + "/bin/java", "-cp", remoting.getPath(),
                        "hudson.remoting.Launcher", "-url", controller, "-name", name, "-secret", "@" + secretFile,
                        "-webSocket", "-workDir", work.toString())
                .redirectErrorStream(true)
                .redirectOutput(work.resolve("agent.log").toFile())
                .start();
    }

    private static void reply(HttpExchange ex, int status, String body) throws IOException {
        byte[] b = body.getBytes(StandardCharsets.UTF_8);
        ex.sendResponseHeaders(status, status == 204 ? -1 : b.length);
        if (status != 204) {
            ex.getResponseBody().write(b);
        }
        ex.close();
    }

    private List<Node> sandboxNodes() {
        List<Node> out = new ArrayList<>();
        for (Node n : r.jenkins.getNodes()) {
            if (n instanceof NetciAgent) {
                out.add(n);
            }
        }
        return out;
    }

    private void waitForNoSandboxNodes() throws InterruptedException {
        for (int i = 0; i < 300 && !sandboxNodes().isEmpty(); i++) {
            Thread.sleep(100);
        }
        assertEquals(List.of(), sandboxNodes(), "a sandbox agent outlived its build");
    }

    @Test
    void aBuildRunsInASandboxThatIsReleasedAfterwards() throws Exception {
        FreeStyleProject p = r.createFreeStyleProject("on-sandbox");
        p.setAssignedLabel(Label.get("netci-linux"));
        FreeStyleBuild b = r.buildAndAssertSuccess(p);
        assertTrue(b.getBuiltOnStr().startsWith("netci-standard-"), b.getBuiltOnStr());
        waitForNoSandboxNodes();
        JSONObject c = claims.get(0);
        assertEquals("standard", c.getString("pool"));
        assertEquals(b.getBuiltOnStr(), c.getString("agent"));
        assertEquals(r.getURL().toString(), c.getString("controller"));
        assertEquals(granted.get(), releases.size(), "a sandbox was not released");
        assertTrue(releases.size() >= 1);
    }

    @Test
    void aPipelineNodeBlockGetsASandbox() throws Exception {
        WorkflowJob p = r.jenkins.createProject(WorkflowJob.class, "pipe");
        p.setDefinition(new CpsFlowDefinition("node('linux') { echo \"on ${env.NODE_NAME}\" }", true));
        WorkflowRun b = r.buildAndAssertSuccess(p);
        r.assertLogContains("on netci-standard-", b);
        waitForNoSandboxNodes();
        assertEquals(granted.get(), releases.size());
    }

    @Test
    void aRefusedClaimLeavesNothingBehindAndJenkinsAsksAgain() throws Exception {
        refuse.set(2);
        FreeStyleProject p = r.createFreeStyleProject("after-refusals");
        p.setAssignedLabel(Label.get("netci-linux"));
        r.buildAndAssertSuccess(p);
        waitForNoSandboxNodes();
        assertTrue(claims.size() >= 3, "claims: " + claims.size());
        // Jenkins may have planned a second agent while the first was refused: every claim the
        // fabric granted must have been released, and nothing else.
        assertEquals(granted.get(), releases.size(), "granted claims and releases differ");
        assertTrue(granted.get() >= 1);
    }

    /**
     * The fabric gives a claim up when its sandbox does not take its binding (a broken warm
     * sandbox, 15 s). The launcher asks what became of its claim while it waits and stops at once,
     * so Jenkins asks again; before, it waited out its connect timeout (lab: a build waited 353 s).
     */
    @Test
    void aClaimTheFabricGaveUpOnIsAbandonedAtOnce() throws Exception {
        dead.set(1);
        FreeStyleProject p = r.createFreeStyleProject("after-a-dead-sandbox");
        p.setAssignedLabel(Label.get("netci-linux"));
        long t0 = System.nanoTime();
        FreeStyleBuild b = p.scheduleBuild2(0).get(100, java.util.concurrent.TimeUnit.SECONDS);
        long seconds = java.util.concurrent.TimeUnit.NANOSECONDS.toSeconds(System.nanoTime() - t0);
        r.assertBuildStatusSuccess(b);
        assertTrue(seconds < 60, "the build waited " + seconds + " s: the dead claim was waited out (connect timeout 120 s)");
        assertTrue(granted.get() >= 2, "granted " + granted.get());
        waitForNoSandboxNodes();
        assertEquals(granted.get(), releases.size(), "a granted claim was not released");
    }

    /**
     * A sandbox that took its binding but whose agent never reaches the controller (the lab froze
     * one; a network cut does the same): the agent connects within a second of its binding, so a
     * claim bound for 20 s with no agent is given up and Jenkins asks again.
     */
    @Test
    void aBoundClaimWhoseAgentNeverArrivesIsGivenUp() throws Exception {
        silent.set(1);
        FreeStyleProject p = r.createFreeStyleProject("after-a-silent-sandbox");
        p.setAssignedLabel(Label.get("netci-linux"));
        long t0 = System.nanoTime();
        FreeStyleBuild b = p.scheduleBuild2(0).get(100, java.util.concurrent.TimeUnit.SECONDS);
        long seconds = java.util.concurrent.TimeUnit.NANOSECONDS.toSeconds(System.nanoTime() - t0);
        r.assertBuildStatusSuccess(b);
        assertTrue(seconds < 50, "the build waited " + seconds + " s for an agent that never came");
        waitForNoSandboxNodes();
        assertEquals(granted.get(), releases.size(), "a granted claim was not released");
    }

    @Test
    void labelsNoPoolServesAreNotProvisioned() throws Exception {
        FreeStyleProject p = r.createFreeStyleProject("elsewhere");
        p.setAssignedLabel(Label.get("windows"));
        p.scheduleBuild2(0);
        Thread.sleep(3000);
        assertEquals(0, claims.size());
        assertEquals(List.of(), sandboxNodes());
    }
}
