package io.netci.jenkins.homesync;

import hudson.init.InitMilestone;
import hudson.init.Initializer;
import hudson.init.Terminator;
import hudson.model.AbstractItem;
import hudson.FilePath;
import hudson.model.Item;
import hudson.model.Job;
import hudson.model.Run;
import jenkins.model.lazy.LazyBuildMixIn;
import hudson.model.User;
import hudson.util.AtomicFileWriter;
import java.io.File;

import hudson.security.ACL;
import hudson.security.ACLContext;
import java.io.ByteArrayInputStream;
import java.io.IOException;
import java.nio.file.Files;
import java.time.Duration;
import java.util.Arrays;
import java.util.Comparator;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicLong;
import java.util.concurrent.locks.ReentrantLock;
import java.util.logging.Level;
import java.util.logging.Logger;
import javax.xml.transform.stream.StreamSource;
import jenkins.model.Jenkins;
import jenkins.model.ModifiableTopLevelItemGroup;

/**
 * Keeps this replica's home the same as the others' (ADR-076): every change to an item (a job, a
 * folder) is published to netci-homesync under the item's version, and every other replica's
 * change is applied here in the order the store gave it.
 *
 * <p>Off unless {@code NETCI_HOMESYNC_URL} is set: a single controller (a cell) has no other home
 * to agree with.
 *
 * <p>Items and users are replicated. What this does not do yet, said here rather than discovered:
 * views, renames and moves are not replicated; a folder's deletion removes its children here and on the others, but the
 * children's own entries are not tombstoned in the store.
 */
public final class HomeSync {
    private static final Logger LOGGER = Logger.getLogger(HomeSync.class.getName());
    private static final Duration POLL = Duration.ofSeconds(25);

    /** Set while this replica applies a change it was given, so the save it causes is not published back. */
    static final ThreadLocal<Boolean> APPLYING = ThreadLocal.withInitial(() -> false);

    private static volatile HomeSync instance;

    final HomeSyncClient client;
    /** The version of each item this home holds, deleted ones included (a re-create starts there). */
    final Map<String, Long> versions = new ConcurrentHashMap<>();
    /**
     * Running builds the store holds a copy of (ADR-079), by path and version. Only recorded, never
     * written into this home: loading one here would resume it.
     */
    final Map<String, Long> runningCopies = new ConcurrentHashMap<>();
    /** The SHA-256 of what this home last published, or applied, for each item. */
    final Map<String, String> published = new ConcurrentHashMap<>();
    /**
     * Applying and publishing one item are one at a time, so a change from the feed is never written
     * over this home's newer save of the same item. Per item, not one lock for everything: applying
     * calls into Jenkins (creating an item takes its group's lock), and a publish runs inside Jenkins'
     * locks (onCreated fires under them). One lock for both deadlocked the lab's r1 for good when a
     * job was created there while the follower created another replica's (ADR-080): the request held
     * the group's lock and waited for this one, the follower held this one and waited for the group's.
     */
    private final Map<String, ReentrantLock> itemLocks = new ConcurrentHashMap<>();
    /**
     * How long a publish, which may hold Jenkins' locks, waits for its item's lock before deferring.
     * Not final only so a test can make it shorter than the store client's 10 s read timeout.
     */
    static volatile Duration publishWait = Duration.ofSeconds(10);
    /** A publish that could not take its item's lock in time: redone by the lease thread, outside Jenkins' locks. */
    record Deferred(byte[] content, boolean delete) {}
    final Map<String, Deferred> deferred = new ConcurrentHashMap<>();

    /** Every change up to this sequence number is applied here. Only ever raised. */
    private final AtomicLong applied = new AtomicLong();
    private Thread follower;
    private Thread renewer;
    private volatile boolean ready;

    HomeSync(HomeSyncClient client) {
        this.client = client;
    }

    /** The running instance, or null when homesync is off (or not started yet). */
    static HomeSync get() {
        return instance;
    }

    /** Edits are refused until the home has caught up with the store: before that, a save could be made from stale content. */
    boolean ready() {
        return ready;
    }

    @Initializer(after = InitMilestone.JOB_CONFIG_ADAPTED)
    public static void start() {
        String url = setting("NETCI_HOMESYNC_URL", "");
        if (url == null) {
            return;
        }
        String tokenFile = setting("NETCI_HOMESYNC_TOKEN_FILE", "/run/secrets/netci-homesync/token");
        HomeSync sync = new HomeSync(new HomeSyncClient(url, tokenFile));
        instance = sync;
        sync.follower = new Thread(sync::run, "netCI homesync follower");
        sync.follower.setDaemon(true);
        sync.follower.start();
        BuildOwnership.forget();
        sync.renewer = new Thread(() -> {
            for (long pass = 1; !Thread.currentThread().isInterrupted(); pass++) {
                BuildOwnership.renewAll(sync);
                if (sync.ready()) {
                    sync.publishDeferred();
                    RunningBuilds.adoptOrphans(sync);
                    if (pass % 6 == 0) {
                        try (ACLContext ignored = ACL.as2(ACL.SYSTEM2)) {
                            AgentsGoHome.pass();
                        } catch (RuntimeException e) {
                            LOGGER.log(Level.WARNING, "netCI homesync: sending agents home failed", e);
                        }
                    }
                }
                sleep(Duration.ofSeconds(5));
            }
        }, "netCI homesync job leases");
        sync.renewer.setDaemon(true);
        sync.renewer.start();
    }

    /** Stop following when Jenkins stops, so nothing is applied to a home that is shutting down. */
    @Terminator
    public static void stop() {
        HomeSync sync = instance;
        instance = null;
        if (sync != null && sync.follower != null) {
            sync.follower.interrupt();
        }
        if (sync != null && sync.renewer != null) {
            sync.renewer.interrupt();
        }
    }

    /** Catch up from the start, then follow. An unreachable store leaves edits refused and is retried. */
    private void run() {
        while (!ready) {
            try {
                bootstrap();
                ready = true;
                LOGGER.info(() -> "netCI homesync: home caught up with the store at seq " + applied.get() + ", " + versions.size() + " items");
            } catch (IOException e) {
                LOGGER.log(Level.WARNING, "netCI homesync: cannot reach the store; edits stay refused until it answers", e);
                sleep(Duration.ofSeconds(3));
            } catch (InterruptedException e) {
                return;
            }
        }
        while (true) {
            try {
                HomeSyncClient.Changes page = client.changes(applied.get(), POLL);
                applyAll(page.changes());
            } catch (IOException e) {
                LOGGER.log(Level.WARNING, "netCI homesync: the feed is unreachable; retrying", e);
                sleep(Duration.ofSeconds(2));
            } catch (InterruptedException e) {
                return;
            } catch (RuntimeException e) {
                // The follower must outlive anything: if it died, this replica would silently stop
                // taking the others' changes (it did once, on a user it could not create).
                LOGGER.log(Level.SEVERE, "netCI homesync: the follower hit an unexpected error; continuing", e);
                sleep(Duration.ofSeconds(2));
            }
        }
    }

    /**
     * The store is the truth for every item it knows; an item only this home has (a replica's first
     * start, or a job its own JCasC created) is published, and if another replica published it first,
     * that one is applied here.
     */
    void bootstrap() throws IOException, InterruptedException {
        Map<String, HomeSyncClient.Change> latest = new HashMap<>();
        long after = 0;
        while (true) {
            HomeSyncClient.Changes page = client.changes(after, Duration.ZERO);
            for (HomeSyncClient.Change c : page.changes()) {
                latest.put(c.path(), c);
                after = c.seq();
            }
            if (page.changes().isEmpty() || after >= page.head()) {
                after = Math.max(after, page.head());
                break;
            }
        }
        // Parents before children.
        List<String> paths = latest.keySet().stream()
                .sorted(Comparator.comparingInt((String p) -> p.split("/").length).thenComparing(p -> p)).toList();
        for (String path : paths) {
            if (path.startsWith(RunningBuilds.RUNNING)) {
                if (!latest.get(path).deleted()) {
                    runningCopies.put(path, latest.get(path).version());
                }
                versions.put(path, latest.get(path).version());
                continue;
            }
            ReentrantLock l = lockOf(path);
            l.lockInterruptibly();
            try {
                HomeSyncClient.Item item = client.get(path);
                if (item.content() == null) {
                    apply(path, item.version(), null);
                } else if (!Arrays.equals(local(path), item.content())) {
                    apply(path, item.version(), item.content());
                } else {
                    versions.put(path, item.version());
                }
            } finally {
                l.unlock();
            }
        }
        advance(after);
        try (ACLContext ignored = ACL.as2(ACL.SYSTEM2)) {
            for (AbstractItem it : Jenkins.get().getAllItems(AbstractItem.class)) {
                if (!latest.containsKey(it.getFullName())) {
                    publishLocked(it.getFullName(), Files.readAllBytes(it.getConfigFile().getFile().toPath()), 0);
                }
            }
            for (User u : User.getAll()) {
                byte[] config = local(userPath(u.getId()));
                if (config != null && !latest.containsKey(userPath(u.getId()))) {
                    publishLocked(userPath(u.getId()), config, 0);
                }
            }
        }
    }

    private ReentrantLock lockOf(String path) {
        return itemLocks.computeIfAbsent(path, k -> new ReentrantLock());
    }

    private void advance(long seq) {
        applied.accumulateAndGet(seq, Math::max);
    }

    /**
     * Apply every change in a page not already held here, in order, each under its item's lock. The
     * follower and an edit catching up may both run through the same changes: whichever takes an
     * item's lock first applies it, the other finds its version already here.
     */
    private void applyAll(List<HomeSyncClient.Change> changes) throws IOException, InterruptedException {
        for (HomeSyncClient.Change c : changes) {
            ReentrantLock l = lockOf(c.path());
            l.lockInterruptibly();
            try {
                applyOne(c);
            } finally {
                l.unlock();
            }
            advance(c.seq());
        }
    }

    private void applyOne(HomeSyncClient.Change c) throws IOException, InterruptedException {
        if (c.path().startsWith(RunningBuilds.RUNNING)) {
            // A copy of a running build: recorded, not fetched, not applied (ADR-079).
            if (c.deleted()) {
                runningCopies.remove(c.path());
            } else {
                runningCopies.put(c.path(), c.version());
            }
            versions.put(c.path(), c.version());
            return;
        }
        if (c.version() > versions.getOrDefault(c.path(), 0L)) {
            // The latest content, which may be newer than this entry: later entries are then no-ops.
            HomeSyncClient.Item item = client.get(c.path());
            if (item.version() > versions.getOrDefault(c.path(), 0L)) {
                try {
                    apply(c.path(), item.version(), item.content());
                } catch (IOException | RuntimeException e) {
                    // One change that cannot be applied must not stop the rest; it is said loudly.
                    LOGGER.log(Level.SEVERE, "netCI homesync: could not apply " + c.path() + " version " + item.version()
                            + "; this home now differs from the others for it", e);
                }
            }
        }
    }

    /** Before an edit: apply everything the store had when the edit's lock was taken. */
    void catchUp(long head) throws IOException, InterruptedException {
        while (applied.get() < head) {
            HomeSyncClient.Changes page = client.changes(applied.get(), Duration.ZERO);
            if (page.changes().isEmpty()) {
                advance(page.head());
                break;
            }
            applyAll(page.changes());
        }
    }

    /**
     * Publish this home's saved config of an item. Under a lock the edit was ordered and caught up,
     * so a refusal here means the order broke; without one (a save no request asked for) another
     * replica's change may have won -- then that change is applied, and the refusal is logged as the
     * lost local change it is.
     */
    void publish(String path, byte[] content, long lockToken) {
        ReentrantLock l = lockOf(path);
        if (!tryLock(l, path, new Deferred(content, false))) {
            return;
        }
        try {
            publishInOrder(path, content, lockToken);
        } finally {
            l.unlock();
        }
    }

    /**
     * The item's lock for a publish, which may be running inside Jenkins' locks: waited for at most
     * {@link #publishWait}, then the publish is deferred rather than risk waiting for good on whoever
     * holds the item's lock while it waits for a lock this thread holds.
     */
    private boolean tryLock(ReentrantLock l, String path, Deferred d) {
        // Already deferred: the item's lock is still contended, and a create is heard twice (created,
        // then saved) -- waiting again would hold the request twice the bound.
        if (deferred.containsKey(path)) {
            deferred.put(path, d);
            return false;
        }
        try {
            if (l.tryLock(publishWait.toMillis(), TimeUnit.MILLISECONDS)) {
                return true;
            }
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
        LOGGER.warning(() -> "netCI homesync: " + path + " is busy being applied; its publish is deferred"
                + " (the other replicas lack this change until it is done)");
        deferred.put(path, d);
        return false;
    }

    /** Called from threads that hold no Jenkins lock: waits for the item's lock. */
    private void publishLocked(String path, byte[] content, long lockToken) throws InterruptedException {
        ReentrantLock l = lockOf(path);
        l.lockInterruptibly();
        try {
            publishInOrder(path, content, lockToken);
        } finally {
            l.unlock();
        }
    }

    /**
     * Deferred publishes, done again from what this home holds now: an item or user is re-read (a
     * later save may have changed it), a finished build is immutable and goes as it was archived.
     */
    void publishDeferred() {
        for (String path : List.copyOf(deferred.keySet())) {
            Deferred d = deferred.remove(path);
            if (d == null) {
                continue;
            }
            ReentrantLock l = lockOf(path);
            try {
                l.lockInterruptibly();
            } catch (InterruptedException e) {
                deferred.putIfAbsent(path, d);
                Thread.currentThread().interrupt();
                return;
            }
            try {
                // Read under the item's lock: read before it, this home's content could be the one
                // a change from the feed was replacing, and publishing it would undo that change in
                // the store while this home kept it (the test caught exactly that).
                byte[] now = d.delete() || path.startsWith(BUILDS) ? d.content() : local(path);
                if (d.delete()) {
                    deleteInOrder(path, 0);
                } else if (now != null) {
                    publishInOrder(path, now, 0);
                }
            } finally {
                l.unlock();
            }
            LOGGER.info(() -> "netCI homesync: deferred publish of " + path + " done");
        }
    }

    private void publishInOrder(String path, byte[] content, long lockToken) {
        // A create can be heard twice (created, then saved): the same content is one version, not two.
        String sum = sha256(content);
        if (versions.containsKey(path) && sum.equals(published.get(path))) {
            return;
        }
        long expect = versions.getOrDefault(path, 0L);
        try {
            versions.put(path, client.put(path, content, expect, lockToken));
            published.put(path, sum);
        } catch (HomeSyncClient.Refused r) {
            LOGGER.severe(() -> "netCI homesync: this replica's change to " + path + " was refused (" + r.code
                    + (r.holder.isEmpty() ? "" : ", held by " + r.holder) + "): " + r.getMessage()
                    + "; the shared version is applied here instead");
            reapply(path);
        } catch (IOException e) {
            LOGGER.log(Level.SEVERE, "netCI homesync: could not publish " + path
                    + "; this home now differs from the others until it is saved again", e);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }

    void publishDelete(String path, long lockToken) {
        ReentrantLock l = lockOf(path);
        if (!tryLock(l, path, new Deferred(null, true))) {
            return;
        }
        try {
            deleteInOrder(path, lockToken);
        } finally {
            l.unlock();
        }
    }

    private void deleteInOrder(String path, long lockToken) {
        long expect = versions.getOrDefault(path, 0L);
        try {
            versions.put(path, client.delete(path, expect, lockToken));
        } catch (HomeSyncClient.Refused r) {
            LOGGER.severe(() -> "netCI homesync: the deletion of " + path + " was refused (" + r.code + "); the shared version is applied");
            reapply(path);
        } catch (IOException e) {
            LOGGER.log(Level.SEVERE, "netCI homesync: could not publish the deletion of " + path, e);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }

    private void reapply(String path) {
        try {
            HomeSyncClient.Item item = client.get(path);
            apply(path, item.version(), item.content());
        } catch (IOException e) {
            LOGGER.log(Level.SEVERE, "netCI homesync: could not re-read " + path + " after a refused change", e);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }

    /** Make this home's item what the store says: created, replaced, or deleted (content null). */
    /**
     * Users are kept the same too, under {@code @users/<id>}: a password, an API token, a user's seed
     * made on one replica and missing on the other would make the replicas two Jenkins to their
     * users -- a token minted on r0 refused by r1, which a script through the VIP reaches at random.
     */
    static final String USERS = "@users/";

    static String userPath(String id) {
        return USERS + id.replace("%", "%25").replace("/", "%2F");
    }

    static String userId(String path) {
        return path.substring(USERS.length()).replace("%2F", "/").replace("%25", "%");
    }

    private void applyUser(String path, byte[] content) throws IOException {
        String id = userId(path);
        User u = User.getById(id, content != null);
        if (content == null) {
            if (u != null) {
                u.delete();
            }
            return;
        }
        if (u.getUserFolder() == null || !new File(u.getUserFolder(), "config.xml").exists()) {
            u.save(); // a user new here has no directory until it is first saved
        }
        File config = new File(u.getUserFolder(), "config.xml");
        try (AtomicFileWriter w = new AtomicFileWriter(config.toPath(), java.nio.charset.StandardCharsets.UTF_8)) {
            try {
                w.write(new String(content, java.nio.charset.StandardCharsets.UTF_8));
                w.commit();
            } finally {
                w.abort(); // a no-op after commit; removes the temporary file if writing failed
            }
        }
        u.load();
    }

    /** Finished builds (ADR-078) are kept the same too, under {@code @builds/<job>/<number>}. */
    static final String BUILDS = "@builds/";

    static String buildPath(Run<?, ?> run) {
        return BUILDS + run.getParent().getFullName() + "/" + run.getNumber();
    }

    /**
     * A finished build another replica ran: its directory written into this home, and the job told
     * the number exists -- without reloading the job. A build is immutable once finished, so one
     * already here is left as it is.
     */
    private void applyBuild(String path, byte[] content) throws IOException, InterruptedException {
        int slash = path.lastIndexOf('/');
        String jobName = path.substring(BUILDS.length(), slash);
        int number = Integer.parseInt(path.substring(slash + 1));
        Job<?, ?> job = Jenkins.get().getItemByFullName(jobName, Job.class);
        if (job == null) {
            LOGGER.warning(() -> "netCI homesync: a build of " + jobName + ", which is not here");
            return;
        }
        if (content == null) {
            Run<?, ?> run = job.getBuildByNumber(number);
            if (run != null) {
                run.delete();
            }
            return;
        }
        File dir = new File(job.getBuildDir(), Integer.toString(number));
        if (dir.exists()) {
            return;
        }
        if (!dir.mkdirs()) {
            throw new IOException("cannot create " + dir);
        }
        // FilePath.untarFrom refuses entries that would land outside the directory.
        new FilePath(dir).untarFrom(new ByteArrayInputStream(content), FilePath.TarCompression.GZIP);
        if (job instanceof LazyBuildMixIn.LazyLoadingJob<?, ?> lazy) {
            lazy.getLazyBuildMixIn()._getRuns().recognizeNumber(number);
        }
        if (job.getNextBuildNumber() <= number) {
            job.updateNextBuildNumber(number + 1);
        }
    }

    void apply(String path, long version, byte[] content) throws IOException, InterruptedException {
        APPLYING.set(true);
        try (ACLContext ignored = ACL.as2(ACL.SYSTEM2)) {
            Jenkins j = Jenkins.get();
            if (path.startsWith(BUILDS)) {
                applyBuild(path, content);
                recordApplied(path, version, content);
                return;
            }
            if (path.startsWith(USERS)) {
                applyUser(path, content);
                recordApplied(path, version, content);
                return;
            }
            Item existing = j.getItemByFullName(path);
            if (content == null) {
                if (existing != null) {
                    existing.delete();
                }
            } else if (existing instanceof AbstractItem ai) {
                ai.updateByXml(new StreamSource(new ByteArrayInputStream(content)));
            } else if (existing == null) {
                int slash = path.lastIndexOf('/');
                Object parent = slash < 0 ? j : j.getItemByFullName(path.substring(0, slash));
                if (!(parent instanceof ModifiableTopLevelItemGroup group)) {
                    LOGGER.warning(() -> "netCI homesync: cannot create " + path + ": its parent is not here");
                    return;
                }
                group.createProjectFromXML(path.substring(slash + 1), new ByteArrayInputStream(content));
            } else {
                LOGGER.warning(() -> "netCI homesync: " + path + " is not an item this replica can update");
                return;
            }
            recordApplied(path, version, content);
        } finally {
            APPLYING.set(false);
        }
    }

    private void recordApplied(String path, long version, byte[] content) {
        versions.put(path, version);
        if (content == null) {
            published.remove(path);
        } else {
            published.put(path, sha256(content));
        }
    }

    static String sha256(byte[] content) {
        try {
            return java.util.HexFormat.of().formatHex(java.security.MessageDigest.getInstance("SHA-256").digest(content));
        } catch (java.security.NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    /**
     * A setting from the environment (how a pod is configured), or the system property of the same
     * name lower-cased with dots (how a test is); null when neither is set and the default is blank.
     */
    static String setting(String env, String dflt) {
        String v = System.getProperty(env.toLowerCase(java.util.Locale.ROOT).replace('_', '.'));
        if (v == null || v.isBlank()) {
            v = System.getenv(env);
        }
        if (v == null || v.isBlank()) {
            v = dflt;
        }
        return v == null || v.isBlank() ? null : v;
    }

    private static byte[] local(String path) {
        try (ACLContext ignored = ACL.as2(ACL.SYSTEM2)) {
            if (path.startsWith(USERS)) {
                User u = User.getById(userId(path), false);
                File config = u == null ? null : new File(u.getUserFolder(), "config.xml");
                return config != null && config.exists() ? Files.readAllBytes(config.toPath()) : null;
            }
            Item it = Jenkins.get().getItemByFullName(path);
            if (it instanceof AbstractItem ai && ai.getConfigFile().exists()) {
                return Files.readAllBytes(ai.getConfigFile().getFile().toPath());
            }
        } catch (IOException e) {
            LOGGER.log(Level.WARNING, "netCI homesync: cannot read " + path, e);
        }
        return null;
    }

    private static void sleep(Duration d) {
        try {
            Thread.sleep(d.toMillis());
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }
}
