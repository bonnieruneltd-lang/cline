package com.royalcourt.redeem;

import com.mojang.logging.LogUtils;
import com.royalcourt.redeem.command.RedeemCommand;
import net.minecraftforge.common.MinecraftForge;
import net.minecraftforge.event.RegisterCommandsEvent;
import net.minecraftforge.eventbus.api.SubscribeEvent;
import net.minecraftforge.fml.ModLoadingContext;
import net.minecraftforge.fml.common.Mod;
import net.minecraftforge.fml.config.ModConfig;
import org.slf4j.Logger;

/**
 * Royal Court Redeemer.
 *
 * <p>A server-side Forge mod for Minecraft 1.20.1 / Forge 47.x. It turns a
 * Royal Court shop key ({@code RC-XXXXXX-XXXXXX}) into the items the buyer paid
 * for in Discord. It is a redemption client only: it never reads or writes the
 * bot database and it never creates an order.
 *
 * <p>Buying happens in Discord. This mod only asks the bot API to redeem a key.
 * The bot refuses every redemption until an admin registers this server's IP in
 * Discord with {@code /server setip}; the mod cannot bypass that gate.
 */
@Mod(RoyalCourtRedeem.MODID)
public final class RoyalCourtRedeem {

    public static final String MODID = "royalcourt";

    private static final Logger LOGGER = LogUtils.getLogger();

    public RoyalCourtRedeem() {
        ModLoadingContext.get().registerConfig(ModConfig.Type.COMMON, RedeemConfig.SPEC);
        MinecraftForge.EVENT_BUS.register(this);
        LOGGER.info(
                "Royal Court Redeemer loaded ({}). Set api.baseUrl and api.token in the config, then "
                        + "an admin must run /server setip in Discord before orders can be claimed.",
                MODID);
    }

    @SubscribeEvent
    public void onRegisterCommands(RegisterCommandsEvent event) {
        RedeemCommand.register(event.getDispatcher());
    }
}
