package com.royalcourt.redeem.item;

import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import java.util.ArrayList;
import java.util.List;
import net.minecraft.resources.ResourceLocation;
import net.minecraft.server.level.ServerPlayer;
import net.minecraft.world.item.EnchantedBookItem;
import net.minecraft.world.item.Item;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.item.enchantment.Enchantment;
import net.minecraft.world.item.enchantment.EnchantmentInstance;
import net.minecraftforge.registries.ForgeRegistries;

/**
 * Turns the API item list into real {@link ItemStack}s and hands them to a
 * player. Runs on the server thread; overflow is dropped at the player's feet.
 *
 * <p>Item keys the shop can send:
 * <ul>
 *   <li>{@code minecraft:<id>} — a vanilla registry lookup.</li>
 *   <li>{@code enchant:<id>:<level>} — a stored-enchantment book built here,
 *       because the payload carries no enchant field.</li>
 *   <li>{@code backport:<id>} — not a vanilla item; mapped to
 *       {@code minecraft:<id>} when a datapack/mod provides it, otherwise
 *       reported as missing rather than silently lost.</li>
 * </ul>
 */
public final class ItemGranting {

    private ItemGranting() {
    }

    public static GrantReport grant(ServerPlayer player, JsonArray items) {
        List<String> granted = new ArrayList<>();
        List<String> missing = new ArrayList<>();

        if (items == null) {
            return new GrantReport(granted, missing);
        }

        for (JsonElement element : items) {
            if (!element.isJsonObject()) {
                continue;
            }
            JsonObject row = element.getAsJsonObject();
            String key = string(row, "key");
            String display = string(row, "display_name");
            int quantity = intOr(row, "quantity", 1);
            if (key.isEmpty() || quantity <= 0) {
                continue;
            }

            ItemStack template = build(key);
            if (template.isEmpty()) {
                missing.add(display.isEmpty() ? key : display + " (" + key + ")");
                continue;
            }

            give(player, template, quantity);
            granted.add((display.isEmpty() ? key : display) + " ×" + quantity);
        }

        return new GrantReport(granted, missing);
    }

    private static void give(ServerPlayer player, ItemStack template, int quantity) {
        int max = Math.max(1, template.getMaxStackSize());
        int remaining = quantity;
        while (remaining > 0) {
            int count = Math.min(max, remaining);
            ItemStack stack = template.copy();
            stack.setCount(count);
            // add() mutates the stack to whatever did not fit; drop that.
            if (!player.getInventory().add(stack) && !stack.isEmpty()) {
                player.drop(stack, false);
            }
            remaining -= count;
        }
    }

    private static ItemStack build(String key) {
        try {
            if (key.startsWith("enchant:")) {
                return buildEnchantedBook(key);
            }
            if (key.startsWith("backport:")) {
                ItemStack direct = byId(new ResourceLocation(key));
                if (!direct.isEmpty()) {
                    return direct;
                }
                return byId(new ResourceLocation("minecraft", key.substring("backport:".length())));
            }
            return byId(new ResourceLocation(key));
        } catch (RuntimeException malformed) {
            return ItemStack.EMPTY;
        }
    }

    private static ItemStack buildEnchantedBook(String key) {
        String[] parts = key.split(":", 3);
        if (parts.length < 3) {
            return ItemStack.EMPTY;
        }
        String enchantId = parts[1].contains(":") ? parts[1] : "minecraft:" + parts[1];
        int level;
        try {
            level = Integer.parseInt(parts[2]);
        } catch (NumberFormatException notANumber) {
            return ItemStack.EMPTY;
        }
        if (level < 1) {
            return ItemStack.EMPTY;
        }

        Enchantment enchantment = ForgeRegistries.ENCHANTMENTS.getValue(new ResourceLocation(enchantId));
        if (enchantment == null) {
            return ItemStack.EMPTY;
        }
        return EnchantedBookItem.createForEnchantment(new EnchantmentInstance(enchantment, level));
    }

    private static ItemStack byId(ResourceLocation id) {
        Item item = ForgeRegistries.ITEMS.getValue(id);
        return item == null ? ItemStack.EMPTY : new ItemStack(item);
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
}
