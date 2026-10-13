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
import java.util.List;
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

    private record Held(String path, long token) {}

    @Initializer(after = InitMilestone.PLUGINS_STARTED)
    public static void register() throws ServletException {
        if (HomeSync.setting("NETCI_HOMESYNC_URL", "") != null) {
            PluginServletFilter.addFilter(new EditOrder());
        }
    }

    /** The lock token the current request holds for {@code path}, or 0 (a save no request ordered). */
    static long tokenFor(String path) {
        Held held = HELD.get();
        return held != null && held.path().equals(path) ? held.token() : 0;
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
        String path = editedItem(http.getMethod(), rest,
                rest.endsWith("/createItem") ? http.getParameter("name") : null);
        HomeSync sync = HomeSync.get();
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
        HELD.set(new Held(path, lock.token()));
        try {
            chain.doFilter(req, rsp);
        } finally {
            HELD.remove();
            try {
                sync.client.unlock(path, lock.token());
            } catch (IOException e) {
                LOGGER.log(Level.WARNING, "netCI homesync: could not release the lock of " + path + "; it expires in " + LOCK_TTL, e);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
        }
    }

    private static void refuse(HttpServletResponse out, int code, String message) throws IOException {
        out.setStatus(code);
        out.setContentType("text/plain;charset=UTF-8");
        out.getWriter().println("netCI: " + message);
    }
}
