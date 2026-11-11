package com.royalcourt.redeem.api;

import com.google.gson.JsonObject;

/**
 * The outcome of one API call: the HTTP status plus the parsed JSON body.
 *
 * <p>{@link #ok()} is true only for a 2xx response with a JSON object; every
 * other case is failed closed by the caller (nothing is granted).
 */
public record ApiResult(int status, JsonObject body, String raw) {

    public boolean ok() {
        return status >= 200 && status < 300 && body != null;
    }

    public String error() {
        return stringField("error");
    }

    public String detail() {
        return stringField("detail");
    }

    private String stringField(String name) {
        if (body != null && body.has(name) && !body.get(name).isJsonNull()) {
            try {
                return body.get(name).getAsString();
            } catch (RuntimeException ignored) {
                return "";
            }
        }
        return "";
    }
}
