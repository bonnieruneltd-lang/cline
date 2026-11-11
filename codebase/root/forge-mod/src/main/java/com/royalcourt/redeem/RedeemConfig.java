package com.royalcourt.redeem;

import net.minecraftforge.common.ForgeConfigSpec;

/**
 * Mod configuration ({@code config/royalcourt-common.toml}).
 *
 * <p>The token lives only in this file — never in source and never in a log
 * line. {@code baseUrl} points at the bot's {@code /api} host (no trailing
 * {@code /api}); {@code serverIp} is optional and only echoed to the bot for
 * diagnostics — the bot independently checks the real connecting address
 * against the IP an admin registered with {@code /server setip}.
 */
public final class RedeemConfig {

    public static final ForgeConfigSpec SPEC;

    private static final ForgeConfigSpec.ConfigValue<String> BASE_URL;
    private static final ForgeConfigSpec.ConfigValue<String> TOKEN;
    private static final ForgeConfigSpec.IntValue TIMEOUT;
    private static final ForgeConfigSpec.ConfigValue<String> SERVER_IP;

    static {
        ForgeConfigSpec.Builder builder = new ForgeConfigSpec.Builder();

        builder.comment("Connection to the Royal Court Discord bot redemption API.").push("api");
        BASE_URL = builder
                .comment("Base URL of the bot API host, for example https://panel.example.com:8080.",
                        "Do not include the /api suffix; the mod appends /api/preview and /api/redeem.")
                .define("baseUrl", "http://127.0.0.1:8080");
        TOKEN = builder
                .comment("The shared bearer token (API_TOKEN on the bot). Required; a blank token disables redemption.")
                .define("token", "");
        TIMEOUT = builder
                .comment("Per-request timeout in seconds.")
                .defineInRange("timeoutSeconds", 15, 1, 120);
        SERVER_IP = builder
                .comment("Optional: this server's public IP, sent to the bot for logging.",
                        "The bot still verifies the real connecting address against /server setip.")
                .define("serverIp", "");
        builder.pop();

        SPEC = builder.build();
    }

    private RedeemConfig() {
    }

    public static String baseUrl() {
        return BASE_URL.get().trim();
    }

    public static String token() {
        return TOKEN.get().trim();
    }

    public static int timeoutSeconds() {
        return TIMEOUT.get();
    }

    public static String serverIp() {
        return SERVER_IP.get().trim();
    }
}
