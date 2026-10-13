package io.netci.jenkins.homesync;

import java.io.ByteArrayInputStream;
import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.KeyStore;
import java.security.cert.CertificateFactory;
import java.time.Duration;
import java.time.Instant;
import java.util.logging.Level;
import java.util.logging.Logger;
import javax.net.ssl.SSLContext;
import javax.net.ssl.TrustManagerFactory;
import net.sf.json.JSONArray;
import net.sf.json.JSONObject;

/**
 * What says a replica is gone before its running builds are adopted (ADR-079). Without one,
 * nothing is adopted: continuing a build whose owner is still running would run it twice.
 */
abstract class Fence {
    private static final Logger LOGGER = Logger.getLogger(Fence.class.getName());

    /** The process that last renewed a lease at {@code lastRenewed} (epoch ms) is not running any more. */
    abstract boolean isGone(String replica, long lastRenewed);

    private static volatile Fence kubernetes;

    static Fence configured() {
        if (!"kubernetes".equals(HomeSync.setting("NETCI_ADOPT_FENCE", ""))) {
            return null;
        }
        if (kubernetes == null) {
            try {
                kubernetes = new KubernetesFence(Path.of(HomeSync.setting("NETCI_KUBERNETES_ACCOUNT",
                        "/var/run/secrets/kubernetes.io/serviceaccount")), HomeSync.setting("NETCI_KUBERNETES_API", "https://kubernetes.default.svc"));
            } catch (IOException | java.security.GeneralSecurityException e) {
                LOGGER.log(Level.SEVERE, "netCI homesync: the Kubernetes fence cannot be set up; nothing is adopted", e);
                return null;
            }
        }
        return kubernetes;
    }

    /**
     * The replica's pod (named as the replica) is gone if Kubernetes says: no such pod; being
     * deleted; not running; its container not running, or started after the lease was last renewed
     * (a new process: the old one is dead); or its machine not Ready. A pod that is merely not ready
     * -- slow, overloaded -- is NOT gone: adopting from a live replica is the failure this prevents.
     * What the API cannot prove is a machine that is partitioned yet alive; a site fences that by
     * power (the supervisor, ADR-060/069).
     */
    static final class KubernetesFence extends Fence {
        private final Path account;
        private final String api;
        private final SSLContext tls;

        KubernetesFence(Path account, String api) throws IOException, java.security.GeneralSecurityException {
            this.account = account;
            this.api = api;
            KeyStore trust = KeyStore.getInstance(KeyStore.getDefaultType());
            trust.load(null, null);
            byte[] ca = Files.readAllBytes(account.resolve("ca.crt"));
            int i = 0;
            for (java.security.cert.Certificate c : CertificateFactory.getInstance("X.509").generateCertificates(new ByteArrayInputStream(ca))) {
                trust.setCertificateEntry("k8s-" + i++, c);
            }
            TrustManagerFactory tmf = TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm());
            tmf.init(trust);
            this.tls = SSLContext.getInstance("TLS");
            this.tls.init(null, tmf.getTrustManagers(), null);
        }

        /**
         * A new connection each attempt, three attempts. The API's Service spreads connections over
         * every control-plane node, and a pooled connection that landed on the machine that died kept
         * landing there: the fence answered "cannot tell" for four minutes (the lab's machine loss).
         */
        private HttpResponse<String> get(String path) throws IOException, InterruptedException {
            String token = Files.readString(account.resolve("token"), StandardCharsets.UTF_8).trim();
            IOException last = null;
            for (int attempt = 0; attempt < 3; attempt++) {
                try (HttpClient http = HttpClient.newBuilder().sslContext(tls).connectTimeout(Duration.ofSeconds(2)).build()) {
                    return http.send(HttpRequest.newBuilder(URI.create(api + path)).timeout(Duration.ofSeconds(3))
                            .header("Authorization", "Bearer " + token).GET().build(), HttpResponse.BodyHandlers.ofString());
                } catch (IOException e) {
                    last = e;
                }
            }
            throw last;
        }

        @Override
        boolean isGone(String replica, long lastRenewed) {
            if (replica == null || replica.isEmpty()) {
                return true; // nobody ever held it
            }
            try {
                String ns = Files.readString(account.resolve("namespace"), StandardCharsets.UTF_8).trim();
                HttpResponse<String> r = get("/api/v1/namespaces/" + ns + "/pods/" + replica);
                if (r.statusCode() == 404) {
                    return true;
                }
                if (r.statusCode() != 200) {
                    return false; // cannot tell: not gone
                }
                JSONObject pod = JSONObject.fromObject(r.body());
                if (pod.getJSONObject("metadata").has("deletionTimestamp")) {
                    return true;
                }
                JSONObject status = pod.getJSONObject("status");
                if (!"Running".equals(status.optString("phase"))) {
                    return true;
                }
                JSONArray containers = status.optJSONArray("containerStatuses");
                for (int i = 0; containers != null && i < containers.size(); i++) {
                    JSONObject c = containers.getJSONObject(i);
                    if (!"jenkins".equals(c.optString("name"))) {
                        continue;
                    }
                    JSONObject running = c.getJSONObject("state").optJSONObject("running");
                    if (running == null || running.isNullObject()) {
                        return true;
                    }
                    if (Instant.parse(running.getString("startedAt")).toEpochMilli() > lastRenewed) {
                        return true; // started after the lease's last renewal: not the process that held it
                    }
                }
                String node = pod.getJSONObject("spec").optString("nodeName");
                HttpResponse<String> n = get("/api/v1/nodes/" + node);
                if (n.statusCode() == 200) {
                    JSONArray conditions = JSONObject.fromObject(n.body()).getJSONObject("status").getJSONArray("conditions");
                    for (int i = 0; i < conditions.size(); i++) {
                        JSONObject c = conditions.getJSONObject(i);
                        if ("Ready".equals(c.optString("type")) && !"True".equals(c.optString("status"))) {
                            return true;
                        }
                    }
                }
                return false;
            } catch (IOException | RuntimeException e) {
                LOGGER.log(Level.WARNING, "netCI homesync: the fence could not tell whether " + replica + " is gone", e);
                return false;
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                return false;
            }
        }
    }
}
