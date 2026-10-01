package io.netci.jenkins.fabric;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import net.sf.json.JSONObject;

/** Talks to netci-fabric (ADR-064) with this cell's token, read from a file at each call. */
final class FabricClient {
    private static final HttpClient HTTP =
            HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(5)).build();

    private final String url;
    private final String tokenFile;

    FabricClient(String url, String tokenFile) {
        this.url = url.endsWith("/") ? url.substring(0, url.length() - 1) : url;
        this.tokenFile = tokenFile;
    }

    /** A claim the fabric granted. */
    record Claim(String id, String pod, boolean warm) {}

    /** The fabric refused, or could not serve, the claim. */
    static final class ClaimException extends IOException {
        private static final long serialVersionUID = 1L;

        ClaimException(int status, String message) {
            super("fabric answered HTTP " + status + ": " + message);
        }
    }

    Claim claim(String pool, String agent, String secret, String controller)
            throws IOException, InterruptedException {
        JSONObject body = new JSONObject()
                .element("pool", pool)
                .element("agent", agent)
                .element("secret", secret)
                .element("controller", controller);
        HttpResponse<String> r = HTTP.send(
                request("/v1/claims")
                        .header("Content-Type", "application/json")
                        .POST(HttpRequest.BodyPublishers.ofString(body.toString(), StandardCharsets.UTF_8))
                        .build(),
                HttpResponse.BodyHandlers.ofString());
        if (r.statusCode() != 200) {
            throw new ClaimException(r.statusCode(), detail(r.body()));
        }
        JSONObject o = JSONObject.fromObject(r.body());
        return new Claim(o.getString("id"), o.optString("pod"), o.optBoolean("warm"));
    }

    void release(String claimId) throws IOException, InterruptedException {
        HttpResponse<String> r = HTTP.send(
                request("/v1/claims/" + claimId).DELETE().build(), HttpResponse.BodyHandlers.ofString());
        if (r.statusCode() != 204 && r.statusCode() != 404) {
            throw new IOException("fabric answered HTTP " + r.statusCode() + " to a release: " + detail(r.body()));
        }
    }

    private HttpRequest.Builder request(String path) throws IOException {
        String token = Files.readString(Path.of(tokenFile), StandardCharsets.UTF_8).trim();
        return HttpRequest.newBuilder(URI.create(url + path))
                .timeout(Duration.ofSeconds(20))
                .header("Authorization", "Bearer " + token);
    }

    private static String detail(String body) {
        try {
            return JSONObject.fromObject(body).optString("detail", body);
        } catch (RuntimeException e) {
            return body.length() > 200 ? body.substring(0, 200) : body;
        }
    }
}
