package io.netci.jenkins.homesync;

import hudson.init.InitMilestone;
import hudson.init.Initializer;
import hudson.util.PluginServletFilter;
import jakarta.servlet.Filter;
import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.ServletRequest;
import jakarta.servlet.ServletResponse;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import java.io.IOException;
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.logging.Level;
import java.util.logging.Logger;

/**
 * Orders an edit across replicas the way one Jenkins orders it: before a request that changes an
 * item runs, the item's lock is taken in netci-homesync and this home catches up with every change
 * made before it; the save the request makes is then published under that lock.
 *
 * <p>So an edit is never made from content another replica has already replaced (ADR-074 E4: the
 * second save silently threw the first away). When the lock is held elsewhere, or the store cannot
 * be reached, the edit is refused with that reason; reads are never affected.
 */
public final class EditOrder implements Filter {
    private static final Logger LOGGER = Logger.getLogger(EditOrder.class.getName());
    private static final Duration LOCK_TTL = Duration.ofSeconds(30);

    /** The actions that change an item's configuration or existence. Builds, stops and reads are not edits. */
    static final Set<String> EDITS = Set.of("configSubmit", "config.xml", "submitDescription", "disable", "enable",
            "doDelete", "createItem");

    private static final ThreadLocal<Held> HELD = new ThreadLocal<>();

    /** The store's locks this request holds, by path: one for an edit, both names for a move. */
    private record Held(Map<String, Long> tokens) {}

    /** A rename or a move (ADR-087): the item's full name before and after. */
    record Move(String from, String to) {
        boolean covers(String path) {
            return path.equals(from) || path.startsWith(from + "/") || path.equals(to) || path.startsWith(to + "/");
        }
    }

    /** The move this request makes, until it is published (see {@link #pendingMove}). */
    private static final ThreadLocal<Move> MOVING = new ThreadLocal<>();

    @Initializer(after = InitMilestone.PLUGINS_STARTED)
    public static void register() throws ServletException {
        if (HomeSync.setting("NETCI_HOMESYNC_URL", "") != null) {
            PluginServletFilter.addFilter(new EditOrder());
        }
    }

    /** What a POST to a root view's page changes (ADR-088); anything under {@code /job/} is the job's. */
    static final Set<String> VIEW_EDITS = Set.of("configSubmit", "config.xml", "submitDescription", "doDelete",
            "addJobToView", "removeJobFromView");

    /**
     * Past {@code /view/<name>} (as many as there are): a job's pages are reached through the view
     * a person is looking at as well, {@code /view/team/job/app/configSubmit} -- the links a view's
     * own page gives. Not skipped, such an edit was not ordered at all.
     */
    static int pastViews(String[] parts, int i) {
        while (i + 2 < parts.length && "view".equals(parts[i])) {
            i += 2;
        }
        return i;
    }

    /**
     * The root view a request changes, as its store path ({@code @views/<name>}): {@code /createView}
     * (name), or an edit on {@code /view/<name>/...} that is not a job's. A view inside a view (a
     * nested view) is part of the outer one's configuration. Null when it changes no view.
     */
    static String editedView(String method, String path, String newViewName) {
        if (!"POST".equals(method)) {
            return null;
        }
        String[] parts = path.split("/");
        int i = 0;
        while (i < parts.length && parts[i].isEmpty()) {
            i++;
        }
        if (i == parts.length - 1 && "createView".equals(parts[i])) {
            return newViewName == null || newViewName.isBlank() ? null : HomeSync.viewPath(newViewName.trim());
        }
        if (i + 2 >= parts.length || !"view".equals(parts[i])) {
            return null;
        }
        String root = URLDecoder.decode(parts[i + 1], StandardCharsets.UTF_8);
        int j = i;
        while (j + 2 < parts.length && "view".equals(parts[j])) {
            j += 2;
        }
        if (j != parts.length - 1 || !(VIEW_EDITS.contains(parts[j]) || "createView".equals(parts[j]))) {
            return null;
        }
        return HomeSync.viewPath(root);
    }

    /** The lock token the current request holds for {@code path}, or 0 (a save no request ordered). */
    static long tokenFor(String path) {
        Held held = HELD.get();
        return held == null ? 0 : held.tokens().getOrDefault(path, 0L);
    }

    /**
     * Whether a save, creation or deletion of {@code path} on this thread happens inside a move not
     * yet published: it is the move's, and published as part of it. Published alone first, the new
     * name would exist in the store, and the move onto it would be refused.
     */
    static boolean pendingMove(String path) {
        Move m = MOVING.get();
        return m != null && m.covers(path);
    }

    /** The move this request made is published (or deferred): later saves are changes of their own. */
    static void movePublished() {
        MOVING.remove();
    }

    /**
     * A rename ({@code /job/a/confirmRename}, newName) or a move ({@code /job/a/move/move},
     * destination {@code /} or {@code /folder}, cloudbees-folder's): null when the request is
     * neither, or names nothing to move to (Jenkins answers it).
     */
    static Move moved(String method, String path, String newName, String destination) {
        if (!"POST".equals(method)) {
            return null;
        }
        String[] parts = path.split("/");
        List<String> names = new ArrayList<>();
        int i = 0;
        while (i < parts.length && parts[i].isEmpty()) {
            i++;
        }
        i = pastViews(parts, i);
        while (i + 1 < parts.length && "job".equals(parts[i])) {
            names.add(URLDecoder.decode(parts[i + 1], StandardCharsets.UTF_8));
            i += 2;
        }
        if (names.isEmpty()) {
            return null;
        }
        String from = String.join("/", names);
        String parent = from.contains("/") ? from.substring(0, from.lastIndexOf('/') + 1) : "";
        String name = names.get(names.size() - 1);
        if (i == parts.length - 1 && "confirmRename".equals(parts[i])) {
            return newName == null || newName.isBlank() ? null : new Move(from, parent + newName.trim());
        }
        if (i == parts.length - 2 && "move".equals(parts[i]) && "move".equals(parts[i + 1])) {
            if (destination == null || !destination.startsWith("/")) {
                return null;
            }
            String folder = destination.substring(1);
            return new Move(from, folder.isEmpty() ? name : folder + "/" + name);
        }
        return null;
    }

    /**
     * The item an edit request names: {@code /job/a/job/b/configSubmit} is {@code a/b};
     * {@code /job/a/createItem?name=c} is {@code a/c}. Null when the request is not an edit.
     */
    static String editedItem(String method, String path, String newItemName) {
        if (!"POST".equals(method)) {
            return null;
        }
        String[] parts = path.split("/");
        List<String> names = new ArrayList<>();
        int i = 0;
        while (i < parts.length && parts[i].isEmpty()) {
            i++;
        }
        // Anything posted to a user's pages changes that user (a password, an API token, the
        // description): two replicas minting tokens for one user at once would drop one of them.
        if (i + 2 < parts.length && "user".equals(parts[i])) {
            return HomeSync.userPath(URLDecoder.decode(parts[i + 1], StandardCharsets.UTF_8));
        }
        i = pastViews(parts, i);
        while (i + 1 < parts.length && "job".equals(parts[i])) {
            names.add(URLDecoder.decode(parts[i + 1], StandardCharsets.UTF_8));
            i += 2;
        }
        if (i != parts.length - 1 || !EDITS.contains(parts[i])) {
            return null;
        }
        if ("createItem".equals(parts[i])) {
            if (newItemName == null || newItemName.isBlank()) {
                return null; // Jenkins refuses it itself
            }
            names.add(newItemName);
        } else if (names.isEmpty()) {
            return null;
        }
        return String.join("/", names);
    }

    @Override
    public void doFilter(ServletRequest req, ServletResponse rsp, FilterChain chain) throws IOException, ServletException {
        HttpServletRequest http = (HttpServletRequest) req;
        String rest = http.getRequestURI().substring(http.getContextPath().length());
        HomeSync sync = HomeSync.get();
        Move move = sync == null ? null : moved(http.getMethod(), rest,
                rest.endsWith("/confirmRename") ? http.getParameter("newName") : null,
                rest.endsWith("/move/move") ? http.getParameter("destination") : null);
        if (move != null && !move.from().equals(move.to())) {
            orderMove(sync, move, http, (HttpServletResponse) rsp, chain);
            return;
        }
        String path = editedItem(http.getMethod(), rest,
                rest.endsWith("/createItem") ? http.getParameter("name") : null);
        boolean view = false;
        if (path == null && sync != null) {
            path = editedView(http.getMethod(), rest, rest.endsWith("/createView") ? http.getParameter("name") : null);
            view = path != null;
        }
        if (path == null || sync == null) {
            chain.doFilter(req, rsp);
            return;
        }
        HttpServletResponse out = (HttpServletResponse) rsp;
        if (!sync.ready()) {
            refuse(out, 503, "this replica has not caught up with the others yet, so " + path
                    + " cannot be changed safely; reads work, try the edit again in a moment");
            return;
        }
        HomeSyncClient.Lock lock;
        try {
            lock = sync.client.lock(path, LOCK_TTL);
            sync.catchUp(lock.head());
        } catch (HomeSyncClient.Refused r) {
            refuse(out, 409, path + " is being changed on replica " + r.holder + "; try again in a moment");
            return;
        } catch (IOException e) {
            LOGGER.log(Level.WARNING, "netCI homesync: refusing an edit of " + path + ": the store is unreachable", e);
            refuse(out, 503, "the replicas' shared order (netci-homesync) cannot be reached, so " + path
                    + " cannot be changed safely; reads work");
            return;
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            refuse(out, 503, "interrupted while ordering the edit of " + path);
            return;
        }
        HELD.set(new Held(Map.of(path, lock.token())));
        try {
            chain.doFilter(req, rsp);
            if (view) {
                // Views live in Jenkins' own config.xml, saved for many reasons: what this request
                // changed is found by comparing every view with what was published, here, under
                // the lock, outside Jenkins' monitor (its save holds it).
                sync.publishViews();
            }
        } finally {
            HELD.remove();
            unlock(sync, path, lock.token());
        }
    }

    /**
     * A rename or move, ordered like an edit but under both names' locks, and asked of the store
     * before Jenkins does it here: Jenkins moves this home's directory at once and cannot be made to
     * take it back, so whatever the store would refuse -- a build under the item running on another
     * replica, the new name taken there, a child being edited there -- is refused first, with its
     * reason. The move itself is published when Jenkins has made it ({@code onLocationChanged}).
     */
    private void orderMove(HomeSync sync, Move move, HttpServletRequest req, HttpServletResponse out, FilterChain chain)
            throws IOException, ServletException {
        if (!sync.ready()) {
            refuse(out, 503, "this replica has not caught up with the others yet, so " + move.from()
                    + " cannot be renamed or moved safely; reads work, try again in a moment");
            return;
        }
        Map<String, Long> tokens = new LinkedHashMap<>();
        try {
            long head = 0;
            for (String p : List.of(move.from(), move.to())) {
                HomeSyncClient.Lock lock = sync.client.lock(p, LOCK_TTL);
                tokens.put(p, lock.token());
                head = Math.max(head, lock.head());
            }
            sync.catchUp(head);
            sync.client.move(move.from(), move.to(), tokens.get(move.from()), tokens.get(move.to()), true);
        } catch (HomeSyncClient.Refused r) {
            // Which lock was another replica's: the first name's, the second's, or (from the dry
            // run, both held) one of the item's children.
            String locked = tokens.isEmpty() ? move.from() : tokens.size() == 1 ? move.to() : "an item in " + move.from();
            unlockAll(sync, tokens);
            switch (r.code) {
                case "LOCKED" -> refuse(out, 409, locked + " is being changed on replica " + r.holder + "; try again in a moment");
                case "BUSY" -> refuse(out, 409, r.getMessage() + "; try again when it has finished");
                case "EXISTS" -> refuse(out, 409, move.to() + " exists on another replica already");
                case "NO_ITEM" -> refuse(out, 409, move.from() + " is not in the replicas' shared store yet, so it cannot be renamed"
                        + " or moved on every replica; try again in a moment");
                default -> chain.doFilter(req, out); // BAD_MOVE: a name Jenkins refuses itself, and says why
            }
            return;
        } catch (IOException e) {
            unlockAll(sync, tokens);
            LOGGER.log(Level.WARNING, "netCI homesync: refusing a move of " + move.from() + ": the store is unreachable", e);
            refuse(out, 503, "the replicas' shared order (netci-homesync) cannot be reached, so " + move.from()
                    + " cannot be renamed or moved safely; reads work");
            return;
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            unlockAll(sync, tokens);
            refuse(out, 503, "interrupted while ordering the move of " + move.from());
            return;
        }
        HELD.set(new Held(Map.copyOf(tokens)));
        MOVING.set(move);
        try {
            chain.doFilter(req, out);
        } finally {
            HELD.remove();
            MOVING.remove();
            unlockAll(sync, tokens);
        }
    }

    private static void unlockAll(HomeSync sync, Map<String, Long> tokens) {
        tokens.forEach((p, t) -> unlock(sync, p, t));
    }

    private static void unlock(HomeSync sync, String path, long token) {
        try {
            sync.client.unlock(path, token);
        } catch (IOException e) {
            LOGGER.log(Level.WARNING, "netCI homesync: could not release the lock of " + path + "; it expires in " + LOCK_TTL, e);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }

    private static void refuse(HttpServletResponse out, int code, String message) throws IOException {
        out.setStatus(code);
        out.setContentType("text/plain;charset=UTF-8");
        out.getWriter().println("netCI: " + message);
    }
}
