package io.netci.jenkins.homesync;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.io.IOException;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.function.BooleanSupplier;
import org.htmlunit.HttpMethod;
import org.htmlunit.Page;
import org.htmlunit.WebRequest;
import org.jenkinsci.plugins.workflow.job.WorkflowJob;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.jvnet.hudson.test.JenkinsRule;
import org.jvnet.hudson.test.junit.jupiter.WithJenkins;

@WithJenkins
class HomeSyncTest {
    static final FakeHomeSync STORE;
    /** What the job's owner (r1) was sent by this replica: "METHOD path | Authorization | crumb | body". */
    static final List<String> AT_OWNER = java.util.Collections.synchronizedList(new java.util.ArrayList<>());

    static {
        try {
            com.sun.net.httpserver.HttpServer owner = com.sun.net.httpserver.HttpServer.create(new java.net.InetSocketAddress("127.0.0.1", 0), 0);
            owner.createContext("/", x -> {
                try (x) {
                    String body = new String(x.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
                    AT_OWNER.add(x.getRequestMethod() + " " + x.getRequestURI().getPath() + " | " + x.getRequestHeaders().getFirst("Authorization")
                            + " | " + x.getRequestHeaders().getFirst("Jenkins-Crumb") + " | " + body);
                    byte[] out;
                    if (x.getRequestURI().getPath().endsWith("/crumbIssuer/api/json")) {
                        out = "{\"crumbRequestField\":\"Jenkins-Crumb\",\"crumb\":\"owner-crumb\"}".getBytes(StandardCharsets.UTF_8);
                        x.sendResponseHeaders(200, out.length);
                    } else {
                        out = new byte[0];
                        x.getResponseHeaders().set("Location", "http://jenkins-1.internal:8080/jenkins/queue/item/42/");
                        x.sendResponseHeaders(201, -1);
                    }
                    if (out.length > 0) {
                        x.getResponseBody().write(out);
                    }
                }
            });
            owner.start();
            System.setProperty("netci.replica.urls", "r1=http://127.0.0.1:" + owner.getAddress().getPort());
            STORE = new FakeHomeSync();
            Path token = Files.createTempFile("homesync", ".token");
            Files.writeString(token, FakeHomeSync.TOKEN);
            System.setProperty("netci.homesync.url", STORE.url());
            System.setProperty("netci.homesync.token.file", token.toString());
            // This replica is "me" to the fake store; r1 is the other, behind the balancer's cookie c1.
            System.setProperty("netci.replica.name", "me");
            System.setProperty("netci.replica.cookies", "me=c0,r1=c1");
        } catch (IOException e) {
            throw new ExceptionInInitializerError(e);
        }
    }

    @AfterEach
    void clean() {
        STORE.reset();
    }

    /** The properties are the JVM's: left set, the test classes after this one would run with homesync on. */
    @org.junit.jupiter.api.AfterAll
    static void off() {
        System.clearProperty("netci.homesync.url");
        System.clearProperty("netci.homesync.token.file");
        System.clearProperty("netci.replica.name");
        System.clearProperty("netci.replica.cookies");
        System.clearProperty("netci.replica.urls");
    }

    private static WorkflowJob pipeline(JenkinsRule j, String name) throws Exception {
        WorkflowJob job = j.createProject(WorkflowJob.class, name);
        job.setDefinition(new org.jenkinsci.plugins.workflow.cps.CpsFlowDefinition("echo 'hi'", true));
        return job;
    }

    @Test
    void aBuildHereTakesTheJobsLeaseAndANumberReservedBeforeItStarts(JenkinsRule j) throws Exception {
        ready();
        WorkflowJob job = pipeline(j, "owned");
        org.jenkinsci.plugins.workflow.job.WorkflowRun b1 = j.buildAndAssertSuccess(job);
        org.jenkinsci.plugins.workflow.job.WorkflowRun b2 = j.buildAndAssertSuccess(job);
        assertEquals("me", STORE.jobOwner.get("owned"));
        assertEquals(List.of(1, 2), STORE.reservedFor("owned"));
        assertEquals(1, b1.getNumber());
        assertEquals(2, b2.getNumber());
    }

    @Test
    void aFinishedBuildIsPublishedAndAnotherReplicasBuildIsListedHere(JenkinsRule j) throws Exception {
        ready();
        WorkflowJob src = pipeline(j, "src");
        j.buildAndAssertSuccess(src);
        await("build #1 published", () -> STORE.items.containsKey("@builds/src/1"));
        byte[] archive;
        synchronized (STORE) {
            archive = STORE.items.get("@builds/src/1").content();
        }
        assertTrue(archive.length > 0);

        // The same build, as r1 would have published it for its own job: listed here, readable.
        WorkflowJob dst = pipeline(j, "dst");
        STORE.changeFrom("r1", "@builds/dst/1", archive);
        await("r1's build listed here", () -> dst.getBuildByNumber(1) != null);
        org.jenkinsci.plugins.workflow.job.WorkflowRun copied = dst.getBuildByNumber(1);
        assertEquals(hudson.model.Result.SUCCESS, copied.getResult());
        assertTrue(copied.getLog().contains("hi"), "the log did not come with the build");
        assertTrue(dst.getNextBuildNumber() >= 2, "the next build here would reuse #1");

        STORE.changeFrom("r1", "@builds/dst/1", (byte[]) null);
        await("r1's deletion of the build here", () -> dst.getBuildByNumber(1) == null);
    }

    @Test
    void buildNowForAnotherReplicasJobIsMadeThereForTheSamePerson(JenkinsRule j) throws Exception {
        // Redirecting the browser's POST failed on the owner: its crumb belonged to this replica's
        // session (403). This replica makes the request there itself, as the same person.
        ready();
        hudson.security.HudsonPrivateSecurityRealm realm = new hudson.security.HudsonPrivateSecurityRealm(false, false, null);
        realm.createAccount("alice", "alice-password-0123456789");
        j.jenkins.setSecurityRealm(realm);
        j.jenkins.setAuthorizationStrategy(new org.jvnet.hudson.test.MockAuthorizationStrategy().grant(jenkins.model.Jenkins.READ,
                hudson.model.Item.READ, hudson.model.Item.BUILD).everywhere().to("alice"));
        String token = hudson.model.User.getById("alice", false).getProperty(jenkins.security.ApiTokenProperty.class)
                .generateNewToken("routing").plainValue;
        pipeline(j, "routed");
        STORE.ownJobAs("r1", "routed");
        AT_OWNER.clear();
        JenkinsRule.WebClient wc = j.createWebClient().withBasicCredentials("alice", token);
        wc.getOptions().setRedirectEnabled(false);
        wc.setThrowExceptionOnFailingStatusCode(false);
        Page made = wc.getPage(new WebRequest(new URL(j.getURL(), "job/routed/build?delay=0sec"), HttpMethod.POST));
        assertEquals(201, made.getWebResponse().getStatusCode(), made.getWebResponse().getContentAsString());
        assertEquals("r1", made.getWebResponse().getResponseHeaderValue("X-Netci-Built-On"));
        assertEquals("/jenkins/queue/item/42/", made.getWebResponse().getResponseHeaderValue("Location"),
                "the owner's own address leaked to the client");
        List<String> sent = new java.util.ArrayList<>(AT_OWNER);
        assertEquals(2, sent.size(), sent.toString());
        String auth = "Basic " + java.util.Base64.getEncoder().encodeToString(("alice:" + token).getBytes(StandardCharsets.UTF_8));
        assertTrue(sent.get(0).startsWith("GET /jenkins/crumbIssuer/api/json | " + auth), sent.get(0));
        assertTrue(sent.get(1).startsWith("POST /jenkins/job/routed/build | " + auth + " | owner-crumb | "), sent.get(1));
        assertTrue(sent.get(1).contains("delay=0sec") && sent.get(1).contains(OwnerRouting.ROUTED + "=1"), sent.get(1));
        assertNull(job(j, "routed").getLastBuild(), "it was also built here");

        // Handed over once already: a reason, not another hop.
        Page again = wc.getPage(new WebRequest(new URL(j.getURL(), "job/routed/build?" + OwnerRouting.ROUTED + "=1"), HttpMethod.POST));
        assertEquals(409, again.getWebResponse().getStatusCode());
        assertTrue(again.getWebResponse().getContentAsString().contains("on replica r1"));
        // A build page this replica does not have goes to the owner (a GET has no crumb to lose).
        assertEquals(302, wc.getPage(new URL(j.getURL(), "job/routed/7/")).getWebResponse().getStatusCode());
    }

    private static WorkflowJob job(JenkinsRule j, String name) {
        return j.jenkins.getItemByFullName(name, WorkflowJob.class);
    }

    @Test
    void aJobOwnedHereIsNotRedirected(JenkinsRule j) throws Exception {
        ready();
        WorkflowJob job = pipeline(j, "mine");
        j.buildAndAssertSuccess(job);
        JenkinsRule.WebClient wc = j.createWebClient();
        wc.getOptions().setRedirectEnabled(false);
        assertEquals(200, wc.getPage(new URL(j.getURL(), "job/mine/1/")).getWebResponse().getStatusCode());
    }

    @Test
    void aJobAnotherReplicaOwnsIsNotScheduledHere(JenkinsRule j) throws Exception {
        ready();
        WorkflowJob job = pipeline(j, "theirs");
        STORE.ownJobAs("r1", "theirs");
        assertNull(job.scheduleBuild2(0), "a build of r1's job was scheduled here");
        Thread.sleep(1000);
        assertNull(job.getLastBuild());
        assertTrue(STORE.reservedFor("theirs").isEmpty());
    }

    @Test
    void numbersContinueAboveEveryReservationAnotherOwnerMade(JenkinsRule j) throws Exception {
        ready();
        WorkflowJob job = pipeline(j, "handed-over");
        synchronized (STORE) {
            STORE.reserved.put("handed-over", new java.util.ArrayList<>(List.of(1, 2, 3, 4, 5))); // a previous owner's
        }
        org.jenkinsci.plugins.workflow.job.WorkflowRun b = j.buildAndAssertSuccess(job);
        assertEquals(6, b.getNumber(), "a number another owner reserved was used again");
        assertEquals(6, STORE.maxReserved("handed-over"));
    }

    @Test
    void withoutTheStoreNothingIsScheduled(JenkinsRule j) throws Exception {
        ready();
        WorkflowJob job = pipeline(j, "no-store");
        STORE.down = true;
        assertNull(job.scheduleBuild2(0), "a build was scheduled with no way to order it");
    }

    private static void await(String what, BooleanSupplier ok) throws InterruptedException {
        long end = System.currentTimeMillis() + 15_000;
        while (!ok.getAsBoolean()) {
            if (System.currentTimeMillis() > end) {
                throw new AssertionError("timed out waiting: " + what);
            }
            Thread.sleep(50);
        }
    }

    private static void ready() throws InterruptedException {
        await("the home to catch up", () -> HomeSync.get() != null && HomeSync.get().ready());
    }

    private static String stored(String path) {
        synchronized (STORE) {
            FakeHomeSync.Entry e = STORE.items.get(path);
            return e == null || e.content() == null ? null : new String(e.content(), StandardCharsets.UTF_8);
        }
    }

    private static long storedVersion(String path) {
        synchronized (STORE) {
            FakeHomeSync.Entry e = STORE.items.get(path);
            return e == null ? 0 : e.version();
        }
    }

    @Test
    void aJobCreatedAndSavedHereIsPublishedVersionByVersion(JenkinsRule j) throws Exception {
        ready();
        WorkflowJob job = j.createProject(WorkflowJob.class, "app");
        await("the create published", () -> storedVersion("app") >= 1);
        long before = storedVersion("app");
        job.setDescription("from here");
        await("the save published", () -> stored("app") != null && stored("app").contains("from here"));
        assertEquals(before + 1, storedVersion("app"));
        // Each change was made from the version this home held: never blind.
        List<FakeHomeSync.Seen> puts = STORE.seen().stream().filter(s -> s.path().equals("app")).toList();
        for (int i = 0; i < puts.size(); i++) {
            assertEquals(i, puts.get(i).ifMatch(), "change " + i + " of app");
        }
    }

    @Test
    void aJobCreatedFromXmlThroughTheApiIsPublishedOnce(JenkinsRule j) throws Exception {
        // POST createItem with a config.xml writes and loads the job without saving it: the save
        // listener alone missed every such job in the first lab run.
        ready();
        j.jenkins.setCrumbIssuer(null); // the body is the config.xml, so no crumb parameter; CSRF is not what this tests
        JenkinsRule.WebClient wc = j.createWebClient();
        WebRequest req = new WebRequest(new URL(j.getURL(), "createItem?name=from-xml"), HttpMethod.POST);
        req.setAdditionalHeader("Content-Type", "application/xml");
        req.setRequestBody("<flow-definition plugin=\"workflow-job\"><description>made by xml</description>"
                + "<keepDependencies>false</keepDependencies><properties/><triggers/><disabled>false</disabled></flow-definition>");
        wc.getPage(req);
        assertNotNull(j.jenkins.getItemByFullName("from-xml"));
        await("the XML create published", () -> stored("from-xml") != null && stored("from-xml").contains("made by xml"));
        Thread.sleep(500);
        List<FakeHomeSync.Seen> puts = STORE.seen().stream().filter(s -> s.path().equals("from-xml")).toList();
        assertEquals(1, puts.size(), "one create, one version: " + puts);
        assertTrue(puts.get(0).lockToken() > 0, "the create was published without the new item's lock");
    }

    @Test
    void aUserSavedHereIsPublishedAndAnotherReplicasUserChangeIsAppliedHere(JenkinsRule j) throws Exception {
        ready();
        hudson.model.User alice = hudson.model.User.getById("alice", true);
        alice.setFullName("Alice From Here");
        alice.save(); // setFullName alone changes memory only; the UI saves after it
        await("alice published", () -> stored("@users/alice") != null && stored("@users/alice").contains("Alice From Here"));

        // r1 renamed her: this replica takes it, and does not publish it back.
        String fromR1 = stored("@users/alice").replace("Alice From Here", "Alice From R1");
        int before = STORE.seen().size();
        STORE.changeFrom("r1", "@users/alice", fromR1);
        await("r1's change to alice here", () -> "Alice From R1".equals(hudson.model.User.getById("alice", false).getFullName()));
        Thread.sleep(500);
        assertTrue(STORE.seen().subList(before, STORE.seen().size()).stream().noneMatch(s -> s.path().equals("@users/alice")),
                "the applied user was published back");

        // A user only r1 has arrives here too.
        STORE.changeFrom("r1", "@users/bob", fromR1.replace("Alice From R1", "Bob"));
        await("bob here", () -> hudson.model.User.getById("bob", false) != null
                && "Bob".equals(hudson.model.User.getById("bob", false).getFullName()));
    }

    private static final String XML = "<flow-definition plugin=\"workflow-job\"><description>%s</description>"
            + "<keepDependencies>false</keepDependencies><properties/><triggers/><disabled>false</disabled></flow-definition>";

    private static Thread createHere(JenkinsRule j, String name, String description) {
        Thread t = new Thread(() -> {
            try (hudson.security.ACLContext ignored = hudson.security.ACL.as2(hudson.security.ACL.SYSTEM2)) {
                j.jenkins.createProjectFromXML(name, new java.io.ByteArrayInputStream(String.format(XML, description).getBytes(StandardCharsets.UTF_8)));
            } catch (IOException e) {
                throw new java.io.UncheckedIOException(e);
            }
        }, "create " + name);
        t.start();
        return t;
    }

    /**
     * The lab's deadlock (ADR-080): a job created here holds Jenkins' item-group lock while it is
     * published; the follower, applying a job another replica created, held the one homesync lock
     * and waited for that group lock. Here the follower is paused in the store fetching r1's job
     * until this replica's create has had the chance to publish -- with one lock for both it never
     * could, and neither thread ever finished.
     */
    @Test
    void aJobCreatedHereWhileAnotherReplicasJobIsBeingAppliedDoesNotDeadlock(JenkinsRule j) throws Exception {
        ready();
        java.util.concurrent.CountDownLatch fetching = new java.util.concurrent.CountDownLatch(1);
        java.util.concurrent.CountDownLatch publishedHere = new java.util.concurrent.CountDownLatch(1);
        STORE.beforeGet = path -> {
            if (path.equals("from-r1")) {
                fetching.countDown();
                try {
                    publishedHere.await(5, java.util.concurrent.TimeUnit.SECONDS);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                }
            }
        };
        STORE.beforeChange = path -> {
            if (path.equals("made-here")) {
                publishedHere.countDown();
            }
        };
        STORE.changeFrom("r1", "from-r1", String.format(XML, "r1"));
        assertTrue(fetching.await(15, java.util.concurrent.TimeUnit.SECONDS), "the follower never fetched r1's job");
        Thread here = createHere(j, "made-here", "here");
        here.join(30_000);
        org.junit.jupiter.api.Assertions.assertFalse(here.isAlive(), "the create here never finished: deadlocked with the follower");
        await("r1's job applied here", () -> j.jenkins.getItemByFullName("from-r1") != null);
        await("the job made here published", () -> stored("made-here") != null);
    }

    /**
     * The same item applied and saved at once: the save waits for the item's lock a bounded time,
     * then is deferred and done later by the lease thread -- the request is never held for good.
     * Both replicas end on r1's job: the store had it first.
     */
    @Test
    void aPublishThatCannotTakeItsItemsLockIsDeferredNotBlockedForGood(JenkinsRule j) throws Exception {
        ready();
        java.time.Duration wait = HomeSync.publishWait;
        HomeSync.publishWait = java.time.Duration.ofSeconds(2);
        try {
            java.util.concurrent.CountDownLatch fetching = new java.util.concurrent.CountDownLatch(1);
            java.util.concurrent.atomic.AtomicBoolean first = new java.util.concurrent.atomic.AtomicBoolean(true);
            // The follower holds "both" for 6 s: past the publish's 2 s, inside the client's 10 s read timeout.
            STORE.beforeGet = path -> {
                if (path.equals("both") && first.getAndSet(false)) {
                    fetching.countDown();
                    try {
                        Thread.sleep(6_000);
                    } catch (InterruptedException e) {
                        Thread.currentThread().interrupt();
                    }
                }
            };
            STORE.changeFrom("r1", "both", String.format(XML, "r1"));
            assertTrue(fetching.await(15, java.util.concurrent.TimeUnit.SECONDS));
            long start = System.currentTimeMillis();
            Thread here = createHere(j, "both", "here");
            here.join(30_000);
            org.junit.jupiter.api.Assertions.assertFalse(here.isAlive(), "the create here was held by the follower");
            long took = System.currentTimeMillis() - start;
            assertTrue(took < 4_500, "the create was held " + took + " ms, past the 2 s bound");
            assertTrue(HomeSync.get().deferred.containsKey("both"), "the publish was not deferred");
        } finally {
            HomeSync.publishWait = wait;
        }
        await("r1's job applied over the one made here", () -> {
            WorkflowJob job = j.jenkins.getItemByFullName("both", WorkflowJob.class);
            return job != null && "r1".equals(job.getDescription());
        });
        await("the deferred publish done", () -> !HomeSync.get().deferred.containsKey("both"));
        assertEquals("r1", stored("both").replaceAll("(?s).*<description>(.*)</description>.*", "$1"));
    }

    @Test
    void aChangeThatCannotBeAppliedDoesNotStopTheOnesAfterIt(JenkinsRule j) throws Exception {
        ready();
        STORE.changeFrom("r1", "broken", "<this is not a job/>");
        STORE.changeFrom("r1", "after-broken", "<flow-definition plugin=\"workflow-job\"><description>still applied</description>"
                + "<keepDependencies>false</keepDependencies><properties/><triggers/><disabled>false</disabled></flow-definition>");
        await("the change after the broken one", () -> j.jenkins.getItemByFullName("after-broken") != null);
        assertNull(j.jenkins.getItemByFullName("broken"));
    }

    @Test
    void anotherReplicasChangeIsAppliedHereWithoutBeingPublishedBack(JenkinsRule j) throws Exception {
        ready();
        STORE.changeFrom("r1", "made-on-r1", "<flow-definition plugin=\"workflow-job\"><description>from r1</description>"
                + "<keepDependencies>false</keepDependencies><properties/><triggers/><disabled>false</disabled></flow-definition>");
        await("r1's job here", () -> j.jenkins.getItemByFullName("made-on-r1") != null);
        WorkflowJob job = j.jenkins.getItemByFullName("made-on-r1", WorkflowJob.class);
        assertEquals("from r1", job.getDescription());
        Thread.sleep(500);
        assertTrue(STORE.seen().stream().noneMatch(s -> s.path().equals("made-on-r1")), "the applied change was published back");

        STORE.changeFrom("r1", "made-on-r1", (String) null);
        await("r1's deletion here", () -> j.jenkins.getItemByFullName("made-on-r1") == null);
    }

    @Test
    void anEditThroughTheUiIsOrderedByTheItemsLock(JenkinsRule j) throws Exception {
        ready();
        j.createProject(WorkflowJob.class, "locked-edit");
        await("published", () -> storedVersion("locked-edit") >= 1);
        JenkinsRule.WebClient wc = j.createWebClient();
        WebRequest req = new WebRequest(new URL(j.getURL(), "job/locked-edit/submitDescription"), HttpMethod.POST);
        req.setRequestParameters(List.of(new org.htmlunit.util.NameValuePair("description", "through the UI")));
        wc.addCrumb(req);
        wc.getPage(req);
        await("published", () -> stored("locked-edit") != null && stored("locked-edit").contains("through the UI"));
        FakeHomeSync.Seen last = STORE.seen().stream().filter(s -> s.path().equals("locked-edit")).reduce((a, b) -> b).orElseThrow();
        assertTrue(last.lockToken() > 0, "the UI edit was published without the item's lock");
        assertNull(STORE.lockHolder.get("locked-edit"), "the lock was not released");
    }

    @Test
    void anEditIsRefusedWhileAnotherReplicaHoldsTheItem(JenkinsRule j) throws Exception {
        ready();
        WorkflowJob job = j.createProject(WorkflowJob.class, "contended");
        job.setDescription("before");
        await("published", () -> stored("contended") != null && stored("contended").contains("before"));
        STORE.lockAs("r1", "contended");
        JenkinsRule.WebClient wc = j.createWebClient();
        wc.setThrowExceptionOnFailingStatusCode(false);
        WebRequest req = new WebRequest(new URL(j.getURL(), "job/contended/submitDescription"), HttpMethod.POST);
        req.setRequestParameters(List.of(new org.htmlunit.util.NameValuePair("description", "racing r1")));
        wc.addCrumb(req);
        Page page = wc.getPage(req);
        assertEquals(409, page.getWebResponse().getStatusCode());
        assertTrue(page.getWebResponse().getContentAsString().contains("being changed on replica r1"));
        assertEquals("before", job.getDescription(), "a refused edit changed the job");
    }

    @Test
    void withoutTheStoreEditsAreRefusedAndReadsStillWork(JenkinsRule j) throws Exception {
        ready();
        j.createProject(WorkflowJob.class, "offline");
        await("published", () -> storedVersion("offline") >= 1);
        STORE.down = true;
        JenkinsRule.WebClient wc = j.createWebClient();
        wc.setThrowExceptionOnFailingStatusCode(false);
        assertEquals(200, wc.goTo("job/offline/").getWebResponse().getStatusCode());
        WebRequest req = new WebRequest(new URL(j.getURL(), "job/offline/disable"), HttpMethod.POST);
        wc.addCrumb(req);
        Page page = wc.getPage(req);
        assertEquals(503, page.getWebResponse().getStatusCode());
        assertTrue(!j.jenkins.getItemByFullName("offline", WorkflowJob.class).isDisabled(), "an unordered edit was applied");
    }

    @Test
    void aBackgroundSaveThatLostToAnotherReplicaTakesTheSharedVersion(JenkinsRule j) throws Exception {
        ready();
        WorkflowJob job = j.createProject(WorkflowJob.class, "bg");
        await("published", () -> storedVersion("bg") >= 1);
        // r1 changed it and this home has not applied that yet (the feed entry is held back by
        // changing the store directly, as a slow follower would see it).
        synchronized (STORE) {
            long v = STORE.items.get("bg").version() + 1;
            STORE.items.put("bg", new FakeHomeSync.Entry(v, ("<flow-definition plugin=\"workflow-job\"><description>r1 won"
                    + "</description><keepDependencies>false</keepDependencies><properties/><triggers/>"
                    + "<disabled>false</disabled></flow-definition>").getBytes(StandardCharsets.UTF_8), "r1"));
        }
        job.setDescription("lost here"); // a save no request ordered, made from the stale version
        await("the shared version applied", () -> "r1 won".equals(j.jenkins.getItemByFullName("bg", WorkflowJob.class).getDescription()));
        assertNotNull(stored("bg"));
        assertTrue(stored("bg").contains("r1 won"), "the stale save replaced r1's");
    }

    // ADR-081: netci-queue's dispatch and lookup through the VIP land on either replica.

    private static String alice(JenkinsRule j) throws Exception {
        hudson.security.HudsonPrivateSecurityRealm realm = new hudson.security.HudsonPrivateSecurityRealm(false, false, null);
        realm.createAccount("alice", "alice-password-0123456789");
        j.jenkins.setSecurityRealm(realm);
        j.jenkins.setAuthorizationStrategy(new org.jvnet.hudson.test.MockAuthorizationStrategy().grant(jenkins.model.Jenkins.READ,
                hudson.model.Item.READ, hudson.model.Item.BUILD).everywhere().to("alice"));
        String token = hudson.model.User.getById("alice", false).getProperty(jenkins.security.ApiTokenProperty.class)
                .generateNewToken("queue").plainValue;
        return "Basic " + java.util.Base64.getEncoder().encodeToString(("alice:" + token).getBytes(StandardCharsets.UTF_8));
    }

    private static java.net.http.HttpResponse<String> netci(JenkinsRule j, String path, String body, String auth) throws Exception {
        java.net.http.HttpRequest.Builder b = java.net.http.HttpRequest.newBuilder(java.net.URI.create(j.getURL() + path)).header("Authorization", auth);
        b = body == null ? b.GET() : b.header("Content-Type", "application/json").POST(java.net.http.HttpRequest.BodyPublishers.ofString(body));
        return java.net.http.HttpClient.newHttpClient().send(b.build(), java.net.http.HttpResponse.BodyHandlers.ofString());
    }

    private static String dispatch(String job, String run) {
        return "{\"job\":\"" + job + "\",\"runId\":\"" + run + "\"}";
    }

    @Test
    void aRunsDispatchAndLookupForAJobAnotherReplicaOwnsAreMadeOnTheOwner(JenkinsRule j) throws Exception {
        ready();
        String auth = alice(j);
        String run = "1b8d6a8e-3f2c-4c7e-9d2a-0a1b2c3d4e5f";
        pipeline(j, "q-owned");
        STORE.ownJobAs("r1", "q-owned");
        AT_OWNER.clear();
        java.net.http.HttpResponse<String> r = netci(j, "netci/dispatch", dispatch("q-owned", run), auth);
        assertEquals(201, r.statusCode(), r.body()); // the owner's own answer (the fake owner says 201)
        assertEquals("r1", r.headers().firstValue("X-Netci-Answered-By").orElse(""));
        java.net.http.HttpResponse<String> l = netci(j, "netci/run?job=q-owned&runId=" + run, null, auth);
        assertEquals("r1", l.headers().firstValue("X-Netci-Answered-By").orElse(""), l.body());
        List<String> sent = new java.util.ArrayList<>(AT_OWNER);
        assertTrue(sent.stream().anyMatch(x -> x.startsWith("POST /jenkins/netci/dispatch | " + auth + " | ") && x.contains(run)), sent.toString());
        assertTrue(sent.stream().anyMatch(x -> x.startsWith("GET /jenkins/netci/run | " + auth)), sent.toString());
        assertEquals(0, j.jenkins.getQueue().getItems().length, "it was queued here too");
        assertNull(job(j, "q-owned").getLastBuild());
    }

    /**
     * The lab's danger: the run's build is on another replica (its owner died, the build waits for
     * adoption), and the run is dispatched again here. It is reported running, not built twice.
     */
    @Test
    void aRunWhoseBuildRunsOnAnotherReplicaIsReportedNotScheduledAgain(JenkinsRule j) throws Exception {
        ready();
        String auth = alice(j);
        String run = "2b8d6a8e-3f2c-4c7e-9d2a-0a1b2c3d4e5f";
        pipeline(j, "q-elsewhere");
        STORE.runReservedBy("r1", run, "q-elsewhere", 1);
        STORE.changeFrom("r1", "@running/q-elsewhere/1", "a running copy");
        java.net.http.HttpResponse<String> r = netci(j, "netci/dispatch", dispatch("q-elsewhere", run), auth);
        assertEquals(200, r.statusCode(), r.body());
        net.sf.json.JSONObject o = net.sf.json.JSONObject.fromObject(r.body());
        assertEquals("started", o.getString("state"), r.body());
        assertEquals(1, o.getJSONObject("build").getInt("number"));
        assertTrue(o.getJSONObject("build").getBoolean("building"));
        java.net.http.HttpResponse<String> l = netci(j, "netci/run?job=q-elsewhere&runId=" + run, null, auth);
        assertEquals(200, l.statusCode(), "a running build reported absent ends its run: " + l.body());
        Thread.sleep(1000);
        assertEquals(0, j.jenkins.getQueue().getItems().length, "scheduled a second time");
        assertNull(job(j, "q-elsewhere").getLastBuild());
    }

    /** A number this replica reserved for the run before it restarted, the build never made: run once. */
    @Test
    void aRunGivenANumberHereButNeverStartedRunsOnce(JenkinsRule j) throws Exception {
        ready();
        String auth = alice(j);
        String run = "3b8d6a8e-3f2c-4c7e-9d2a-0a1b2c3d4e5f";
        WorkflowJob job = pipeline(j, "q-lost");
        STORE.runReservedBy("me", run, "q-lost", 1);
        java.net.http.HttpResponse<String> r = netci(j, "netci/dispatch", dispatch("q-lost", run), auth);
        assertEquals(200, r.statusCode(), r.body());
        await("the run built", () -> job.getLastBuild() != null && !job.getLastBuild().isBuilding());
        assertEquals(2, job.getLastBuild().getNumber(), "the number given up stays taken");
        synchronized (STORE) {
            assertEquals(2, STORE.runs.get(run).number());
        }
        assertEquals(200, netci(j, "netci/dispatch", dispatch("q-lost", run), auth).statusCode());
        Thread.sleep(1000);
        assertEquals(1, job.getBuilds().size(), "dispatched again, built again");
    }

    /** Whatever slips past the lookups, the store refuses the run a second number: the item is cancelled. */
    @Test
    void aSecondItemForARunThatHasANumberIsCancelled(JenkinsRule j) throws Exception {
        ready();
        WorkflowJob job = pipeline(j, "q-dup");
        String run = "4b8d6a8e-3f2c-4c7e-9d2a-0a1b2c3d4e5f";
        STORE.runReservedBy("r1", run, "q-dup", 7);
        j.jenkins.getQueue().schedule2(job, 0, List.of(new io.netci.jenkins.NetciRunAction(run)));
        await("the second item cancelled", () -> j.jenkins.getQueue().getItems().length == 0);
        Thread.sleep(1500);
        assertNull(job.getLastBuild(), "a run built twice");
    }

    /** The store unreachable: "try again", never "refused" (409) nor "absent" -- each ends the run. */
    @Test
    void withTheStoreUnreachableARunIsNeitherRefusedNorAbsent(JenkinsRule j) throws Exception {
        ready();
        String auth = alice(j);
        pipeline(j, "q-down");
        String run = "5b8d6a8e-3f2c-4c7e-9d2a-0a1b2c3d4e5f";
        STORE.down = true;
        assertEquals(503, netci(j, "netci/dispatch", dispatch("q-down", run), auth).statusCode());
        assertEquals(503, netci(j, "netci/run?job=q-down&runId=" + run, null, auth).statusCode());
    }
}
