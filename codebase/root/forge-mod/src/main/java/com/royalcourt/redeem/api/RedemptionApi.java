package com.royalcourt.redeem.api;

import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import com.royalcourt.redeem.RedeemConfig;
import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.UUID;

/**
 * Blocking HTTP client for the bot's redemption API.
 *
 * <p>Call these methods from a worker thread only — never the server thread —
 * then apply the result back on the server thread. The class is stateless and
 * thread-safe.
 */
public final class RedemptionApi {

    private static final HttpClient CLIENT = HttpClient.newBuilder()
            .connectTimeout(Duration.ofSeconds(10))
            .build();

    private RedemptionApi() {
    }

    /** {@code POST /api/preview} — lists the items without consuming the key. */
    public static ApiResult preview(String key, UUID uuid, String name)
            throws IOException, InterruptedException {
        return post("/api/preview", key, uuid, name);
    }

    /** {@code POST /api/redeem} — consumes the key and returns the items. */
    public static ApiResult redeem(String key, UUID uuid, String name)
            throws IOException, InterruptedException {
        return post("/api/redeem", key, uuid, name);
    }

    private static ApiResult post(String path, String key, UUID uuid, String name)
            throws IOException, InterruptedException {
        String base = RedeemConfig.baseUrl();
        String token = RedeemConfig.token();
        if (base.isEmpty() || token.isEmpty()) {
            throw new IOException("The Royal Court API URL or token is not configured.");
        }

        JsonObject body = new JsonObject();
        body.addProperty("redeem_key", key);
        body.addProperty("player_uuid", uuid.toString());
        body.addProperty("player_name", name);
        String serverIp = RedeemConfig.serverIp();
        if (!serverIp.isEmpty()) {
            body.addProperty("server_ip", serverIp);
        }

        HttpRequest request;
        try {
            request = HttpRequest.newBuilder(URI.create(trimTrailingSlash(base) + path))
                    .timeout(Duration.ofSeconds(RedeemConfig.timeoutSeconds()))
                    .header("Authorization", "Bearer " + token)
                    .header("Content-Type", "application/json")
                    .POST(HttpRequest.BodyPublishers.ofString(body.toString()))
                    .build();
        } catch (IllegalArgumentException badUrl) {
            throw new IOException("Invalid API base URL: " + base, badUrl);
        }

        HttpResponse<String> response = CLIENT.send(request, HttpResponse.BodyHandlers.ofString());
        return new ApiResult(response.statusCode(), parse(response.body()), response.body());
    }

    private static JsonObject parse(String raw) {
        try {
            if (raw == null || raw.isBlank()) {
                return new JsonObject();
            }
            var element = JsonParser.parseString(raw);
            return element.isJsonObject() ? element.getAsJsonObject() : new JsonObject();
        } catch (RuntimeException malformed) {
            return new JsonObject();
        }
    }

    private static String trimTrailingSlash(String value) {
        return value.endsWith("/") ? value.substring(0, value.length() - 1) : value;
    }
}
