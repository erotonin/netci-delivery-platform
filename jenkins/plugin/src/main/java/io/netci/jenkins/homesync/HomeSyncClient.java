package io.netci.jenkins.homesync;

import java.io.IOException;
import java.net.URI;
import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import net.sf.json.JSONArray;
import net.sf.json.JSONObject;

/** Talks to netci-homesync (ADR-076) with this replica's token, read from a file at each call. */
final class HomeSyncClient {
    private static final HttpClient HTTP =
            HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(3)).build();

    private final String url;
    private final String tokenFile;

    HomeSyncClient(String url, String tokenFile) {
        this.url = url.endsWith("/") ? url.substring(0, url.length() - 1) : url;
        this.tokenFile = tokenFile;
    }

    /**
     * An item as the store has it. {@code content} is null when it never existed or was deleted;
     * {@code updatedBy} is the replica that last wrote it, {@code updatedAt} when (epoch ms, the
     * database's clock).
     */
    record Item(long version, byte[] content, String updatedBy, long updatedAt) {
        Item(long version, byte[] content) {
            this(version, content, "", 0);
        }
    }

    /** One entry of the store's feed. */
    record Change(long seq, String path, long version, boolean deleted, String origin) {}

    /** A page of the feed and its head. */
    record Changes(List<Change> changes, long head) {}

    /** A lock on an item, and the feed's head when it was taken. */
    record Lock(long token, long head) {}

    /** A job's lease (ADR-078): the epoch it is held under, for how long, and the highest number reserved. */
    record JobLease(long epoch, long expiresInMs, int maxReserved) {}

    /** Who holds a job's lease, whether it is live, and how long until (negative: since) it expires. */
    record JobOwner(String owner, boolean live, long expiresInMs) {}

    JobOwner jobOwner(String job) throws IOException, InterruptedException {
        HttpResponse<String> r = HTTP.send(request("/v1/jobs/owner?job=" + enc(job)).GET().build(), HttpResponse.BodyHandlers.ofString());
        if (r.statusCode() != 200) {
            throw new IOException("homesync answered HTTP " + r.statusCode() + " for the owner of " + job);
        }
        JSONObject o = json(r.body());
        return new JobOwner(o.optString("owner"), o.optLong("expiresInMs") > 0, o.optLong("expiresInMs"));
    }

    /** The store refused a change: {@code code} is VERSION_CONFLICT, LOCKED or LOCK_LOST. */
    static final class Refused extends IOException {
        private static final long serialVersionUID = 1L;
        final String code;
        final long current;
        final String holder;

        Refused(String code, long current, String holder, String message) {
            super(message);
            this.code = code;
            this.current = current;
            this.holder = holder;
        }
    }

    Item get(String path) throws IOException, InterruptedException {
        HttpResponse<byte[]> r = HTTP.send(request("/v1/items?path=" + enc(path)).GET().build(),
                HttpResponse.BodyHandlers.ofByteArray());
        long version = Long.parseLong(r.headers().firstValue("X-Version").orElse("0"));
        if (r.statusCode() == 404) {
            return new Item(version, null);
        }
        if (r.statusCode() != 200) {
            throw new IOException("homesync answered HTTP " + r.statusCode() + " for " + path);
        }
        return new Item(version, r.body(), r.headers().firstValue("X-Updated-By").orElse(""),
                Long.parseLong(r.headers().firstValue("X-Updated-At").orElse("0")));
    }

    long put(String path, byte[] content, long expect, long lockToken) throws IOException, InterruptedException {
        return change(request("/v1/items?path=" + enc(path)).PUT(HttpRequest.BodyPublishers.ofByteArray(content)),
                expect, lockToken);
    }

    long delete(String path, long expect, long lockToken) throws IOException, InterruptedException {
        return change(request("/v1/items?path=" + enc(path)).DELETE(), expect, lockToken);
    }

    private long change(HttpRequest.Builder b, long expect, long lockToken) throws IOException, InterruptedException {
        b.header("If-Match", Long.toString(expect));
        if (lockToken > 0) {
            b.header("X-Lock-Token", Long.toString(lockToken));
        }
        HttpResponse<String> r = HTTP.send(b.build(), HttpResponse.BodyHandlers.ofString());
        JSONObject o = json(r.body());
        if (r.statusCode() == 200) {
            return o.getLong("version");
        }
        if (r.statusCode() == 409 || r.statusCode() == 423) {
            throw new Refused(o.optString("code"), o.optLong("current"), o.optString("holder"), o.optString("message"));
        }
        throw new IOException("homesync answered HTTP " + r.statusCode() + ": " + o.optString("message", r.body()));
    }

    Lock lock(String path, Duration ttl) throws IOException, InterruptedException {
        HttpResponse<String> r = HTTP.send(
                request("/v1/locks?path=" + enc(path) + "&ttl=" + ttl.toMillis() + "ms")
                        .POST(HttpRequest.BodyPublishers.noBody()).build(),
                HttpResponse.BodyHandlers.ofString());
        JSONObject o = json(r.body());
        if (r.statusCode() == 200) {
            return new Lock(o.getLong("token"), o.getLong("head"));
        }
        if (r.statusCode() == 423) {
            throw new Refused("LOCKED", 0, o.optString("holder"), o.optString("message"));
        }
        throw new IOException("homesync answered HTTP " + r.statusCode() + " to a lock of " + path);
    }

    JobLease claimJob(String job, Duration ttl) throws IOException, InterruptedException {
        HttpResponse<String> r = HTTP.send(
                request("/v1/jobs/claim?job=" + enc(job) + "&ttl=" + ttl.toMillis() + "ms")
                        .POST(HttpRequest.BodyPublishers.noBody()).build(),
                HttpResponse.BodyHandlers.ofString());
        JSONObject o = json(r.body());
        if (r.statusCode() == 200) {
            return new JobLease(o.getLong("epoch"), o.getLong("expiresInMs"), o.optInt("maxReserved"));
        }
        if (r.statusCode() == 423) {
            throw new Refused("OWNED", o.optLong("epoch"), o.optString("owner"), "owned by " + o.optString("owner"));
        }
        throw new IOException("homesync answered HTTP " + r.statusCode() + " to a claim of " + job);
    }

    void reserve(String job, int number, long epoch) throws IOException, InterruptedException {
        reserve(job, number, epoch, null);
    }

    /**
     * The number for a build, recording the netCI run it is for (runId, or null): refused with
     * RUN_TAKEN when that run has a number already, Refused.current holding it (ADR-081).
     */
    void reserve(String job, int number, long epoch, String runId) throws IOException, InterruptedException {
        HttpResponse<String> r = HTTP.send(
                request("/v1/jobs/reserve?job=" + enc(job) + "&number=" + number + "&epoch=" + epoch + (runId == null ? "" : "&run=" + enc(runId)))
                        .POST(HttpRequest.BodyPublishers.noBody()).build(),
                HttpResponse.BodyHandlers.ofString());
        if (r.statusCode() == 200) {
            return;
        }
        JSONObject o = json(r.body());
        if (r.statusCode() == 409 && "RUN_TAKEN".equals(o.optString("code"))) {
            throw new Refused("RUN_TAKEN", o.optLong("number"), o.optString("job"), o.optString("message"));
        }
        if (r.statusCode() == 409) {
            throw new Refused(o.optString("code"), o.optLong("max"), "", o.optString("message"));
        }
        throw new IOException("homesync answered HTTP " + r.statusCode() + " to a reservation of " + job + " #" + number);
    }

    /** Where a netCI run was given its number, or null when it has none (ADR-081). */
    record RunReservation(String job, int number, String replica, long reservedAt) {}

    RunReservation run(String runId) throws IOException, InterruptedException {
        HttpResponse<String> r = HTTP.send(request("/v1/runs?id=" + enc(runId)).GET().build(), HttpResponse.BodyHandlers.ofString());
        if (r.statusCode() == 404) {
            return null;
        }
        if (r.statusCode() != 200) {
            throw new IOException("homesync answered HTTP " + r.statusCode() + " for run " + runId);
        }
        JSONObject o = json(r.body());
        return new RunReservation(o.getString("job"), o.getInt("number"), o.getString("replica"), o.getLong("reservedAt"));
    }

    /** Forget the run's number, its build having never existed; false when it no longer had it. */
    boolean releaseRun(String runId, String job, int number) throws IOException, InterruptedException {
        HttpResponse<Void> r = HTTP.send(request("/v1/runs?id=" + enc(runId) + "&job=" + enc(job) + "&number=" + number).DELETE().build(),
                HttpResponse.BodyHandlers.discarding());
        if (r.statusCode() == 204) {
            return true;
        }
        if (r.statusCode() == 404) {
            return false;
        }
        throw new IOException("homesync answered HTTP " + r.statusCode() + " to a release of run " + runId);
    }

    void unlock(String path, long token) throws IOException, InterruptedException {
        HTTP.send(request("/v1/locks?path=" + enc(path) + "&token=" + token).DELETE().build(),
                HttpResponse.BodyHandlers.discarding());
    }

    Changes changes(long after, Duration wait) throws IOException, InterruptedException {
        HttpResponse<String> r = HTTP.send(
                request("/v1/changes?after=" + after + "&wait=" + wait.toMillis() + "ms")
                        .timeout(wait.plusSeconds(10)).GET().build(),
                HttpResponse.BodyHandlers.ofString());
        if (r.statusCode() != 200) {
            throw new IOException("homesync answered HTTP " + r.statusCode() + " to the feed");
        }
        JSONObject o = JSONObject.fromObject(r.body());
        List<Change> list = new ArrayList<>();
        JSONArray a = o.getJSONArray("changes");
        for (int i = 0; i < a.size(); i++) {
            JSONObject c = a.getJSONObject(i);
            list.add(new Change(c.getLong("seq"), c.getString("path"), c.getLong("version"),
                    c.getBoolean("deleted"), c.getString("origin")));
        }
        return new Changes(list, o.getLong("head"));
    }

    private HttpRequest.Builder request(String path) throws IOException {
        // Read at each call: a rotated token takes effect without a restart. Never logged.
        String token = Files.readString(Path.of(tokenFile), StandardCharsets.UTF_8).trim();
        return HttpRequest.newBuilder(URI.create(url + path))
                .timeout(Duration.ofSeconds(10))
                .header("Authorization", "Bearer " + token);
    }

    private static String enc(String s) {
        return URLEncoder.encode(s, StandardCharsets.UTF_8);
    }

    private static JSONObject json(String body) {
        try {
            return JSONObject.fromObject(body);
        } catch (RuntimeException e) {
            return new JSONObject();
        }
    }
}
