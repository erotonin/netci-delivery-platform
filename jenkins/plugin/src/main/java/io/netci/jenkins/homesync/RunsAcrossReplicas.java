package io.netci.jenkins.homesync;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.logging.Logger;

/**
 * netci-queue's dispatch and lookup of a run, answered for every replica (ADR-081).
 *
 * <p>netci-queue reaches the replicas through the VIP, so each call lands on either of them, and it
 * took two answers as final: a 409 refused the run for good, and "absent" for a started run marked
 * it finished. Through the VIP both happened without a fault: a dispatch on the replica that does
 * not own the job was refused by Jenkins' queue (only the owner schedules), and a lookup on it did
 * not see a build running on the owner. Worse, after the owner died with the run's build on it, a
 * dispatch again found nothing on the new owner and scheduled a second build of the run, which the
 * adoption of the first then made two.
 *
 * <p>So a call goes to the job's live owner when that is another replica, as "Build now" does; and
 * a replica that does not hold a run asks the store, where the number the run was given is recorded
 * (one per run, refused twice: the second item is cancelled) with the build's running copy or its
 * finished build.
 */
public final class RunsAcrossReplicas {
    private static final Logger LOGGER = Logger.getLogger(RunsAcrossReplicas.class.getName());
    private static final HttpClient HTTP = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(3)).build();
    /** Marks a call forwarded once: the owner answers it, never forwards it again. */
    public static final String ROUTED = "X-Netci-Routed";

    private RunsAcrossReplicas() {}

    /** Whether runs are shared between replicas here (homesync on and caught up). */
    public static boolean active() {
        HomeSync sync = HomeSync.get();
        return sync != null && sync.ready();
    }

    /**
     * The job's live owner when it is another replica this one can reach, else null (answer here).
     *
     * @throws IOException the store cannot say: the caller must not answer as if it knew
     */
    public static String ownerElsewhere(String job) throws IOException, InterruptedException {
        HomeSync sync = HomeSync.get();
        if (sync == null || !sync.ready()) {
            return null;
        }
        HomeSyncClient.JobOwner o = sync.client.jobOwner(job);
        String me = OwnerRouting.me();
        if (!o.live() || o.owner().isEmpty() || o.owner().equals(me)) {
            return null;
        }
        return OwnerRouting.pairs(HomeSync.setting("NETCI_REPLICA_URLS", "")).containsKey(o.owner()) ? o.owner() : null;
    }

    public record Answer(int status, String body) {}

    /**
     * The same call made on the owner with the caller's own credential (an API token: users are
     * replicated, and API tokens need no CSRF crumb), marked as routed.
     */
    public static Answer forward(String owner, String method, String pathAndQuery, String body, String authorization)
            throws IOException, InterruptedException {
        String base = OwnerRouting.pairs(HomeSync.setting("NETCI_REPLICA_URLS", "")).get(owner);
        if (base == null) {
            throw new IOException("no address for replica " + owner);
        }
        HttpRequest.Builder b = HttpRequest.newBuilder(URI.create(base + pathAndQuery)).timeout(Duration.ofSeconds(15))
                .header("Authorization", authorization).header(ROUTED, "1");
        if ("POST".equals(method)) {
            b.header("Content-Type", "application/json").POST(HttpRequest.BodyPublishers.ofString(body == null ? "" : body));
        } else {
            b.GET();
        }
        HttpResponse<String> r = HTTP.send(b.build(), HttpResponse.BodyHandlers.ofString());
        return new Answer(r.statusCode(), r.body());
    }

    /** Where a run is that this replica does not hold. */
    public record Elsewhere(boolean started, String job, int number, String replica) {}

    /**
     * The run as the store knows it, or null when it has no number (or had one only on a replica
     * that died before the build existed: that number is let go, so the run may be scheduled).
     */
    public static Elsewhere find(String runId) throws IOException, InterruptedException {
        HomeSync sync = HomeSync.get();
        if (sync == null || !sync.ready()) {
            return null;
        }
        HomeSyncClient.RunReservation res = sync.client.run(runId);
        if (res == null) {
            return null;
        }
        String build = res.job() + "/" + res.number();
        HomeSyncClient.Item running = sync.client.get(RunningBuilds.RUNNING + build);
        if (running.content() != null) {
            return new Elsewhere(true, res.job(), res.number(), running.updatedBy());
        }
        if (sync.client.get(HomeSync.BUILDS + build).content() != null) {
            // Finished, not yet applied here: said to be running until a replica that has it
            // answers with its result -- this one cannot tell the result, and must not guess.
            return new Elsewhere(true, res.job(), res.number(), res.replica());
        }
        // A number and nothing else: the build is about to exist, or its replica died before it did.
        boolean gone;
        if (res.replica().equals(OwnerRouting.me())) {
            gone = true; // reserved by this replica, which does not have it now (it restarted)
        } else {
            Fence fence = Fence.configured();
            gone = fence != null && fence.isGone(res.replica(), res.reservedAt());
        }
        if (!gone) {
            return new Elsewhere(false, res.job(), res.number(), res.replica());
        }
        if (sync.client.releaseRun(runId, res.job(), res.number())) {
            LOGGER.warning(() -> "netCI homesync: run " + runId + " had " + res.job() + " #" + res.number() + " on " + res.replica()
                    + ", which is gone and never started it; the run may be scheduled again");
        }
        return null;
    }
}
