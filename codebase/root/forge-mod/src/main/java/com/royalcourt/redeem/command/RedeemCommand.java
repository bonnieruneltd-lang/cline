package com.royalcourt.redeem.command;

import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.mojang.brigadier.CommandDispatcher;
import com.mojang.brigadier.arguments.StringArgumentType;
import com.mojang.brigadier.context.CommandContext;
import com.mojang.brigadier.exceptions.CommandSyntaxException;
import com.royalcourt.redeem.RedeemConfig;
import com.royalcourt.redeem.api.ApiResult;
import com.royalcourt.redeem.api.RedemptionApi;
import com.royalcourt.redeem.item.GrantReport;
import com.royalcourt.redeem.item.ItemGranting;
import java.util.ArrayList;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import net.minecraft.ChatFormatting;
import net.minecraft.commands.CommandSourceStack;
import net.minecraft.commands.Commands;
import net.minecraft.network.chat.ClickEvent;
import net.minecraft.network.chat.Component;
import net.minecraft.network.chat.HoverEvent;
import net.minecraft.network.chat.MutableComponent;
import net.minecraft.network.chat.Style;
import net.minecraft.server.MinecraftServer;
import net.minecraft.server.level.ServerPlayer;

/**
 * {@code /redeem <key>} previews an order and shows a clickable Claim button;
 * {@code /redeem <key> confirm} consumes the key and grants the items.
 *
 * <p>Network calls run on a small daemon pool; every UI update and every item
 * grant is marshalled back onto the server thread through
 * {@link MinecraftServer#execute(Runnable)}. Nothing is granted when the API
 * fails closed (bad token, key already used, or the server IP is not the one an
 * admin registered in Discord).
 */
public final class RedeemCommand {

    private static final ExecutorService WORKER = Executors.newFixedThreadPool(2, runnable -> {
        Thread thread = new Thread(runnable, "royalcourt-redeem");
        thread.setDaemon(true);
        return thread;
    });

    private RedeemCommand() {
    }

    public static void register(CommandDispatcher<CommandSourceStack> dispatcher) {
        dispatcher.register(Commands.literal("redeem")
                .then(Commands.argument("key", StringArgumentType.string())
                        .executes(RedeemCommand::preview)
                        .then(Commands.literal("confirm")
                                .executes(RedeemCommand::confirm))));
    }

    private static int preview(CommandContext<CommandSourceStack> context) {
        ServerPlayer player = playerOrNull(context);
        if (player == null || !configured(player)) {
            return 0;
        }
        String key = StringArgumentType.getString(context, "key").trim().toUpperCase();
        UUID uuid = player.getUUID();
        String name = player.getGameProfile().getName();
        MinecraftServer server = player.getServer();
        player.sendSystemMessage(info("Checking key " + key + " …"));
        CompletableFuture.runAsync(() -> {
            try {
                ApiResult result = RedemptionApi.preview(key, uuid, name);
                server.execute(() -> showPreview(player, key, result));
            } catch (Exception failure) {
                server.execute(() -> player.sendSystemMessage(error("Redemption failed: " + message(failure))));
            }
        }, WORKER);
        return 1;
    }

    private static int confirm(CommandContext<CommandSourceStack> context) {
        ServerPlayer player = playerOrNull(context);
        if (player == null || !configured(player)) {
            return 0;
        }
        String key = StringArgumentType.getString(context, "key").trim().toUpperCase();
        UUID uuid = player.getUUID();
        String name = player.getGameProfile().getName();
        MinecraftServer server = player.getServer();
        player.sendSystemMessage(info("Claiming " + key + " …"));
        CompletableFuture.runAsync(() -> {
            try {
                ApiResult result = RedemptionApi.redeem(key, uuid, name);
                server.execute(() -> finish(player, key, result));
            } catch (Exception failure) {
                server.execute(() -> player.sendSystemMessage(error("Redemption failed: " + message(failure))));
            }
        }, WORKER);
        return 1;
    }

    private static void showPreview(ServerPlayer player, String key, ApiResult result) {
        if (result.status() != 200) {
            fail(player, result);
            return;
        }
        JsonArray items = items(result.body());
        player.sendSystemMessage(header("Order " + key + " — review before claiming:"));
        for (Component line : itemLines(items)) {
            player.sendSystemMessage(line);
        }
        MutableComponent button = Component.literal("[ Claim Now ]").withStyle(Style.EMPTY
                .withColor(ChatFormatting.GREEN)
                .withBold(true)
                .withClickEvent(new ClickEvent(ClickEvent.Action.RUN_COMMAND, "/redeem " + key + " confirm"))
                .withHoverEvent(new HoverEvent(HoverEvent.Action.SHOW_TEXT,
                        Component.literal("Grant these items. The key is single-use."))));
        player.sendSystemMessage(button);
    }

    private static void finish(ServerPlayer player, String key, ApiResult result) {
        if (result.status() != 200) {
            fail(player, result);
            return;
        }
        GrantReport report = ItemGranting.grant(player, items(result.body()));
        player.sendSystemMessage(success("Order " + key + " claimed."));
        if (!report.granted().isEmpty()) {
            player.sendSystemMessage(info("Granted: " + String.join(", ", report.granted())));
        }
        if (!report.missing().isEmpty()) {
            player.sendSystemMessage(error("Not available on this server — contact staff: "
                    + String.join(", ", report.missing())));
        }
    }

    private static void fail(ServerPlayer player, ApiResult result) {
        switch (result.status()) {
            case 403 -> {
                String errorCode = result.error();
                if ("server_not_registered".equals(errorCode)) {
                    player.sendSystemMessage(error("Redemption is locked: an admin must enter this "
                            + "server's IP in Discord with /server setip before any order can be claimed."));
                } else if ("ip_not_allowed".equals(errorCode)) {
                    player.sendSystemMessage(error("This server's IP is not the one registered in "
                            + "Discord, so the order was not claimed."));
                } else {
                    player.sendSystemMessage(error("Redemption refused (403). " + result.detail()));
                }
            }
            case 409 -> player.sendSystemMessage(error("This key has already been claimed."));
            case 404 -> player.sendSystemMessage(error("No order matches that redeem key."));
            case 401 -> player.sendSystemMessage(error("The bot rejected the API token. Check the mod config."));
            case 400 -> player.sendSystemMessage(error("The request was rejected as malformed."));
            default -> player.sendSystemMessage(error("Redemption failed (HTTP " + result.status() + "). "
                    + result.detail()));
        }
    }

    private static List<Component> itemLines(JsonArray items) {
        List<Component> lines = new ArrayList<>();
        for (JsonElement element : items) {
            if (!element.isJsonObject()) {
                continue;
            }
            JsonObject row = element.getAsJsonObject();
            String display = string(row, "display_name");
            String key = string(row, "key");
            int quantity = intOr(row, "quantity", 1);
            lines.add(Component.literal(" • ").withStyle(ChatFormatting.DARK_GRAY)
                    .append(Component.literal(quantity + " × ").withStyle(ChatFormatting.GOLD))
                    .append(Component.literal(display.isEmpty() ? key : display).withStyle(ChatFormatting.WHITE)));
        }
        if (lines.isEmpty()) {
            lines.add(Component.literal(" • (no items)").withStyle(ChatFormatting.GRAY));
        }
        return lines;
    }

    private static ServerPlayer playerOrNull(CommandContext<CommandSourceStack> context) {
        try {
            return context.getSource().getPlayerOrException();
        } catch (CommandSyntaxException notAPlayer) {
            context.getSource().sendFailure(Component.literal("Only a player can redeem a shop key."));
            return null;
        }
    }

    private static boolean configured(ServerPlayer player) {
        if (RedeemConfig.baseUrl().isEmpty() || RedeemConfig.token().isEmpty()) {
            player.sendSystemMessage(error("The Royal Court API is not configured. Set api.baseUrl "
                    + "and api.token in config/royalcourt-common.toml."));
            return false;
        }
        return true;
    }

    private static JsonArray items(JsonObject body) {
        return body != null && body.has("items") && body.get("items").isJsonArray()
                ? body.getAsJsonArray("items")
                : new JsonArray();
    }

    private static String string(JsonObject row, String name) {
        return row.has(name) && !row.get(name).isJsonNull() ? row.get(name).getAsString() : "";
    }

    private static int intOr(JsonObject row, String name, int fallback) {
        try {
            return row.has(name) && !row.get(name).isJsonNull() ? row.get(name).getAsInt() : fallback;
        } catch (RuntimeException notANumber) {
            return fallback;
        }
    }

    private static String message(Exception failure) {
        String text = failure.getMessage();
        return text == null || text.isBlank() ? failure.getClass().getSimpleName() : text;
    }

    private static MutableComponent header(String text) {
        return Component.literal(text).withStyle(ChatFormatting.GOLD, ChatFormatting.BOLD);
    }

    private static MutableComponent info(String text) {
        return Component.literal("Royal Court » ").withStyle(ChatFormatting.GOLD)
                .append(Component.literal(text).withStyle(ChatFormatting.GRAY));
    }

    private static MutableComponent success(String text) {
        return Component.literal("Royal Court » ").withStyle(ChatFormatting.GOLD)
                .append(Component.literal(text).withStyle(ChatFormatting.GREEN));
    }

    private static MutableComponent error(String text) {
        return Component.literal("Royal Court » ").withStyle(ChatFormatting.GOLD)
                .append(Component.literal(text).withStyle(ChatFormatting.RED));
    }
}

