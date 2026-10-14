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
                case "POST /v1/jobs/reserve" -> reserve(x, q.get("job"), Integer.parseInt(q.get("number")), Long.parseLong(q.get("epoch")));
                default -> reply(x, 404, new JSONObject());
            }
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }

    private synchronized void getItem(HttpExchange x, String path) throws IOException {
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

    private synchronized void reserve(HttpExchange x, String job, int number, long epoch) throws IOException {
        if (!"me".equals(jobOwner.get(job)) || jobEpoch.get(job) != epoch) {
            reply(x, 409, new JSONObject().element("code", "NOT_OWNER").element("message", "not the owner"));
            return;
        }
        if (number <= maxReserved(job)) {
            reply(x, 409, new JSONObject().element("code", "NUMBER_TAKEN").element("max", maxReserved(job)));
            return;
        }
        reserved.computeIfAbsent(job, k -> new ArrayList<>()).add(number);
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
        reply(x, 200, new JSONObject().element("changes", list).element("head", feed.size()));
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
