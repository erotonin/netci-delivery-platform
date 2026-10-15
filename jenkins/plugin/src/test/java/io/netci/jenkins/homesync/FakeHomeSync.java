package io.netci.jenkins.homesync;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import net.sf.json.JSONArray;
import net.sf.json.JSONObject;

/**
 * netci-homesync's semantics in memory, for the plugin's tests: versions with compare-and-set, a
 * feed, locks with tokens. The real store's are tested against PostgreSQL in internal/homesync.
 * Every change the plugin makes is recorded with the headers it carried.
 */
final class FakeHomeSync {
    static final String TOKEN = "test-replica-token-0123456789abcdef0123";

    record Entry(long version, byte[] content, String by) {}

    record Seen(String method, String path, long ifMatch, long lockToken) {}

    final Map<String, Entry> items = new HashMap<>();
    final List<JSONObject> feed = new ArrayList<>();
    final Map<String, String> lockHolder = new HashMap<>();
    final Map<String, Long> lockToken = new HashMap<>();
    /** job -> owner; a job's epoch; numbers reserved per job. */
    final Map<String, String> jobOwner = new HashMap<>();
    final Map<String, Long> jobEpoch = new HashMap<>();
    final Map<String, List<Integer>> reserved = new HashMap<>();
    /** run id -> its reservation (ADR-081). */
    record RunAt(String job, int number, String replica, long at) {}

    final Map<String, RunAt> runs = new HashMap<>();
    /** Jobs with a build running on some replica, by job: a move over them is refused BUSY (ADR-087). */
    final Map<String, String> busy = new HashMap<>();
    /** Every path read, in order. */
    final List<String> reads = new ArrayList<>();
    /** Feed entries up to here are gone (ADR-083); see compact(). */
    long compactedThrough;
    private long epochs = 1000;
    final List<Seen> seen = new ArrayList<>();
    volatile boolean down;
    /** Called with the item's path before an item is read or changed, outside the store's monitor (a test's pause). */
    volatile java.util.function.Consumer<String> beforeGet = path -> {};
    volatile java.util.function.Consumer<String> beforeChange = path -> {};
    private long tokens = 100;
    private final HttpServer server;

    FakeHomeSync() throws IOException {
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/", this::handle);
        server.setExecutor(java.util.concurrent.Executors.newCachedThreadPool());
        server.start();
    }

    String url() {
        return "http://127.0.0.1:" + server.getAddress().getPort();
    }

    synchronized void reset() {
        items.clear();
        feed.clear();
        lockHolder.clear();
        lockToken.clear();
        seen.clear();
        jobOwner.clear();
        jobEpoch.clear();
        reserved.clear();
        runs.clear();
        busy.clear();
        reads.clear();
        compactedThrough = 0;
        down = false;
        beforeGet = path -> {};
        beforeChange = path -> {};
    }

    /** Another replica changes an item, as the store would record it. */
    synchronized long changeFrom(String replica, String path, String content) {
        return changeFrom(replica, path, content == null ? null : content.getBytes(StandardCharsets.UTF_8));
    }

    synchronized long changeFrom(String replica, String path, byte[] content) {
        long v = items.containsKey(path) ? items.get(path).version() + 1 : 1;
        items.put(path, new Entry(v, content, replica));
        feed.add(new JSONObject().element("seq", feed.size() + 1).element("path", path).element("version", v)
                .element("deleted", content == null).element("origin", replica));
        notifyAll();
        return v;
    }

    synchronized void lockAs(String replica, String path) {
        lockHolder.put(path, replica);
        lockToken.put(path, ++tokens);
    }

    synchronized List<Seen> seen() {
        return new ArrayList<>(seen);
    }

    private void handle(HttpExchange x) throws IOException {
        try (x) {
            if (down) {
                reply(x, 503, new JSONObject().element("code", "UNAVAILABLE"));
                return;
            }
            if (!("Bearer " + TOKEN).equals(x.getRequestHeaders().getFirst("Authorization"))) {
                reply(x, 401, new JSONObject().element("code", "UNAUTHORIZED"));
                return;
            }
            Map<String, String> q = query(x.getRequestURI().getRawQuery());
            String route = x.getRequestMethod() + " " + x.getRequestURI().getPath();
            switch (route) {
                case "GET /v1/items" -> {
                    beforeGet.accept(q.get("path"));
                    getItem(x, q.get("path"));
                }
                case "PUT /v1/items", "DELETE /v1/items" -> {
                    beforeChange.accept(q.get("path"));
                    change(x, q.get("path"));
                }
                case "POST /v1/locks" -> lock(x, q.get("path"));
                case "DELETE /v1/locks" -> unlock(x, q.get("path"), Long.parseLong(q.get("token")));
                case "GET /v1/changes" -> changes(x, Long.parseLong(q.get("after")), q.get("wait"));
                case "POST /v1/jobs/claim" -> claim(x, q.get("job"));
                case "GET /v1/jobs/owner" -> owner(x, q.get("job"));
                case "POST /v1/jobs/reserve" -> reserve(x, q.get("job"), Integer.parseInt(q.get("number")), Long.parseLong(q.get("epoch")), q.get("run"));
                case "GET /v1/runs" -> run(x, q.get("id"));
                case "DELETE /v1/runs" -> releaseRun(x, q.get("id"), q.get("job"), Integer.parseInt(q.get("number")));
                case "POST /v1/moves" -> move(x, q.get("from"), q.get("to"), "true".equals(q.get("dryRun")));
                default -> reply(x, 404, new JSONObject());
            }
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        } catch (RuntimeException e) {
            // The fake's own mistake: said, or the plugin sees only a closed connection.
            e.printStackTrace();
            throw e;
        }
    }

    private synchronized void getItem(HttpExchange x, String path) throws IOException {
        reads.add(path);
        Entry e = items.get(path);
        x.getResponseHeaders().set("X-Version", Long.toString(e == null ? 0 : e.version()));
        if (e != null) {
            x.getResponseHeaders().set("X-Updated-By", e.by());
            x.getResponseHeaders().set("X-Updated-At", Long.toString(System.currentTimeMillis()));
        }
        if (e == null || e.content() == null) {
            reply(x, 404, new JSONObject().element("code", "NO_ITEM"));
            return;
        }
        x.sendResponseHeaders(200, e.content().length);
        try (OutputStream o = x.getResponseBody()) {
            o.write(e.content());
        }
    }

    private synchronized void change(HttpExchange x, String path) throws IOException {
        long expect = Long.parseLong(x.getRequestHeaders().getFirst("If-Match"));
        String tok = x.getRequestHeaders().getFirst("X-Lock-Token");
        long token = tok == null ? 0 : Long.parseLong(tok);
        byte[] body = x.getRequestBody().readAllBytes();
        seen.add(new Seen(x.getRequestMethod(), path, expect, token));
        String holder = lockHolder.get(path);
        if (token != 0 && (holder == null || !holder.equals("me") || lockToken.get(path) != token)) {
            reply(x, 409, new JSONObject().element("code", "LOCK_LOST").element("message", "lock lost"));
            return;
        }
        if (token == 0 && holder != null && !holder.equals("me")) {
            reply(x, 423, new JSONObject().element("code", "LOCKED").element("holder", holder).element("message", "locked"));
            return;
        }
        long current = items.containsKey(path) ? items.get(path).version() : 0;
        if (current != expect) {
            reply(x, 409, new JSONObject().element("code", "VERSION_CONFLICT").element("current", current).element("message", "stale"));
            return;
        }
        boolean deleted = "DELETE".equals(x.getRequestMethod());
        items.put(path, new Entry(current + 1, deleted ? null : body, "me"));
        feed.add(new JSONObject().element("seq", feed.size() + 1).element("path", path).element("version", current + 1)
                .element("deleted", deleted).element("origin", "me"));
        notifyAll();
        reply(x, 200, new JSONObject().element("version", current + 1));
    }

    synchronized void ownJobAs(String replica, String job) {
        jobOwner.put(job, replica);
        jobEpoch.put(job, ++epochs);
    }

    synchronized int maxReserved(String job) {
        return reserved.getOrDefault(job, List.of()).stream().mapToInt(Integer::intValue).max().orElse(0);
    }

    synchronized List<Integer> reservedFor(String job) {
        return new ArrayList<>(reserved.getOrDefault(job, List.of()));
    }

    private synchronized void owner(HttpExchange x, String job) throws IOException {
        String owner = jobOwner.get(job);
        reply(x, 200, new JSONObject().element("job", job).element("owner", owner == null ? "" : owner)
                .element("epoch", jobEpoch.getOrDefault(job, 0L)).element("expiresInMs", owner == null ? 0 : 15000));
    }

    private synchronized void claim(HttpExchange x, String job) throws IOException {
        if (items.containsKey(job) && items.get(job).content() == null) {
            reply(x, 410, new JSONObject().element("code", "GONE").element("message", "gone"));
            return;
        }
        String owner = jobOwner.get(job);
        if (owner != null && !owner.equals("me")) {
            reply(x, 423, new JSONObject().element("code", "OWNED").element("owner", owner).element("epoch", jobEpoch.get(job)));
            return;
        }
        if (owner == null) {
            ownJobAs("me", job);
        }
        reply(x, 200, new JSONObject().element("job", job).element("owner", "me").element("epoch", jobEpoch.get(job))
                .element("expiresInMs", 15000).element("maxReserved", maxReserved(job)));
    }

    /** Another replica gave a run a number. */
    synchronized void runReservedBy(String replica, String runId, String job, int number) {
        reserved.computeIfAbsent(job, k -> new ArrayList<>()).add(number);
        runs.put(runId, new RunAt(job, number, replica, System.currentTimeMillis()));
    }

    private synchronized void run(HttpExchange x, String id) throws IOException {
        RunAt r = runs.get(id);
        if (r == null) {
            reply(x, 404, new JSONObject().element("code", "NO_RUN"));
            return;
        }
        reply(x, 200, new JSONObject().element("job", r.job()).element("number", r.number()).element("replica", r.replica())
                .element("reservedAt", r.at()));
    }

    private synchronized void releaseRun(HttpExchange x, String id, String job, int number) throws IOException {
        RunAt r = runs.get(id);
        if (r == null || !r.job().equals(job) || r.number() != number) {
            reply(x, 404, new JSONObject().element("code", "NO_RUN"));
            return;
        }
        runs.remove(id);
        x.sendResponseHeaders(204, -1);
    }

    private synchronized void reserve(HttpExchange x, String job, int number, long epoch, String run) throws IOException {
        if (run != null && runs.containsKey(run)) {
            RunAt had = runs.get(run);
            reply(x, 409, new JSONObject().element("code", "RUN_TAKEN").element("job", had.job()).element("number", had.number()));
            return;
        }
        for (String p = job; p != null; p = p.contains("/") ? p.substring(0, p.lastIndexOf('/')) : null) {
            String holder = lockHolder.get(p);
            if (holder != null && !holder.equals("me")) {
                reply(x, 423, new JSONObject().element("code", "LOCKED").element("holder", holder).element("message", "locked"));
                return;
            }
        }
        if (!"me".equals(jobOwner.get(job)) || jobEpoch.get(job) == null || jobEpoch.get(job) != epoch) {
            reply(x, 409, new JSONObject().element("code", "NOT_OWNER").element("message", "not the owner"));
            return;
        }
        if (number <= maxReserved(job)) {
            reply(x, 409, new JSONObject().element("code", "NUMBER_TAKEN").element("max", maxReserved(job)));
            return;
        }
        reserved.computeIfAbsent(job, k -> new ArrayList<>()).add(number);
        if (run != null) {
            runs.put(run, new RunAt(job, number, "me", System.currentTimeMillis()));
        }
        reply(x, 200, new JSONObject().element("job", job).element("number", number));
    }

    private synchronized void lock(HttpExchange x, String path) throws IOException {
        String holder = lockHolder.get(path);
        if (holder != null && !holder.equals("me")) {
            reply(x, 423, new JSONObject().element("code", "LOCKED").element("holder", holder));
            return;
        }
        lockHolder.put(path, "me");
        lockToken.put(path, ++tokens);
        reply(x, 200, new JSONObject().element("token", tokens).element("head", feed.size()));
    }

    private synchronized void unlock(HttpExchange x, String path, long token) throws IOException {
        if ("me".equals(lockHolder.get(path)) && lockToken.get(path) == token) {
            lockHolder.remove(path);
            lockToken.remove(path);
        }
        x.sendResponseHeaders(204, -1);
    }

    private synchronized void changes(HttpExchange x, long after, String wait) throws IOException, InterruptedException {
        long deadline = System.currentTimeMillis() + (wait == null ? 0 : Long.parseLong(wait.replace("ms", "")));
        while (feed.size() <= after && System.currentTimeMillis() < deadline) {
            wait(Math.max(1, Math.min(200, deadline - System.currentTimeMillis())));
        }
        JSONArray list = new JSONArray();
        for (int i = (int) after; i < feed.size(); i++) {
            list.add(feed.get(i));
        }
        reply(x, 200, new JSONObject().element("changes", list).element("head", feed.size()).element("compactedThrough", compactedThrough));
    }

    /** Another replica renames or moves an item, as the store would (ADR-087). */
    synchronized void moveFrom(String replica, String from, String to) {
        String refused = moveIn(replica, from, to, false);
        if (refused != null) {
            throw new IllegalStateException(refused);
        }
    }

    private static boolean under(String path, String root) {
        return path.equals(root) || path.startsWith(root + "/");
    }

    /** The move, or the code it is refused with. */
    private String moveIn(String replica, String from, String to, boolean dryRun) {
        if (from.equals(to) || to.startsWith(from + "/")) {
            return "BAD_MOVE";
        }
        Entry f = items.get(from);
        if (f == null || f.content() == null) {
            return "NO_ITEM";
        }
        if (items.containsKey(to) && items.get(to).content() != null) {
            return "EXISTS";
        }
        for (Map.Entry<String, String> l : lockHolder.entrySet()) {
            if ((under(l.getKey(), from) || l.getKey().equals(to)) && !l.getValue().equals(replica)) {
                return "LOCKED:" + l.getValue();
            }
        }
        for (Map.Entry<String, String> b : busy.entrySet()) {
            if (under(b.getKey(), from)) {
                return "BUSY:" + b.getValue();
            }
        }
        if (dryRun) {
            return null;
        }
        List<String> moving = new ArrayList<>();
        for (Map.Entry<String, Entry> e : items.entrySet()) {
            if (e.getValue().content() != null && (under(e.getKey(), from) || e.getKey().startsWith("@builds/" + from + "/"))) {
                moving.add(e.getKey());
            }
        }
        moving.sort(java.util.Comparator.comparing((String p) -> p.startsWith("@")).thenComparingInt(p -> p.split("/").length)
                .thenComparing(p -> p));
        java.util.function.Function<String, String> renamed = p -> p.startsWith("@builds/" + from + "/")
                ? "@builds/" + to + p.substring(("@builds/" + from).length()) : to + p.substring(from.length());
        Map<String, byte[]> contents = new HashMap<>();
        moving.forEach(p -> contents.put(p, items.get(p).content()));
        record(replica, from, null, to);
        for (String p : moving) {
            record(replica, renamed.apply(p), contents.get(p), null);
        }
        for (String p : moving) {
            if (!p.equals(from)) {
                record(replica, p, null, null);
            }
        }
        for (Map<String, ?> m : List.of(jobOwner, jobEpoch, reserved)) {
            @SuppressWarnings("unchecked")
            Map<String, Object> mm = (Map<String, Object>) m;
            for (String k : new ArrayList<>(mm.keySet())) {
                if (under(k, from)) {
                    mm.put(to + k.substring(from.length()), mm.remove(k));
                }
            }
        }
        notifyAll();
        return null;
    }

    private void record(String replica, String path, byte[] content, String movedTo) {
        long v = items.containsKey(path) ? items.get(path).version() + 1 : 1;
        items.put(path, new Entry(v, content, replica));
        JSONObject c = new JSONObject().element("seq", feed.size() + 1).element("path", path).element("version", v)
                .element("deleted", content == null).element("origin", replica);
        if (movedTo != null) {
            c.element("movedTo", movedTo);
        }
        feed.add(c);
    }

    private synchronized void move(HttpExchange x, String from, String to, boolean dryRun) throws IOException {
        for (String[] name : new String[][] {{from, "X-Lock-Token"}, {to, "X-To-Lock-Token"}}) {
            String tok = x.getRequestHeaders().getFirst(name[1]);
            if (tok != null && (!"me".equals(lockHolder.get(name[0])) || lockToken.get(name[0]) != Long.parseLong(tok))) {
                reply(x, 409, new JSONObject().element("code", "LOCK_LOST").element("message", "lock lost"));
                return;
            }
        }
        if (!dryRun) {
            seen.add(new Seen("MOVE", from + " -> " + to, 0, 0));
        }
        String refused = moveIn("me", from, to, dryRun);
        if (refused == null) {
            reply(x, 200, new JSONObject().element("version", items.containsKey(to) ? items.get(to).version() : 0)
                    .element("sha256", dryRun ? "" : HomeSync.sha256(items.get(to).content()))
                    .element("fromVersion", items.get(from).version()));
            return;
        }
        String[] code = refused.split(":", 2);
        int status = switch (code[0]) {
            case "NO_ITEM" -> 404;
            case "BAD_MOVE" -> 400;
            case "LOCKED" -> 423;
            default -> 409;
        };
        reply(x, status, new JSONObject().element("code", code[0]).element("holder", code.length > 1 ? code[1] : "")
                .element("message", code[0].equals("BUSY") ? from + " #1 is running on " + code[1] + "; an item is not renamed or moved while it builds"
                        : code[0]));
    }

    private static void reply(HttpExchange x, int code, JSONObject body) throws IOException {
        byte[] b = body.toString().getBytes(StandardCharsets.UTF_8);
        x.getResponseHeaders().set("Content-Type", "application/json");
        x.sendResponseHeaders(code, b.length);
        try (OutputStream o = x.getResponseBody()) {
            o.write(b);
        }
    }

    private static Map<String, String> query(String raw) {
        Map<String, String> q = new HashMap<>();
        if (raw != null) {
            for (String kv : raw.split("&")) {
                int eq = kv.indexOf('=');
                if (eq > 0) {
                    q.put(kv.substring(0, eq), URLDecoder.decode(kv.substring(eq + 1), StandardCharsets.UTF_8));
                }
            }
        }
        return q;
    }
}
