"""
cogs/server.py
==============
Admin-only registration of the Minecraft server IP that gates the redemption
API. Until an admin runs ``/server setip`` the Forge mod cannot claim any shop
order: ``/api/redeem`` answers ``403 server_not_registered``.
"""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

import embeds
import serverip
from checks import admin_only


class Server(commands.Cog):
    """🔐 Minecraft server allow-list for the redemption API."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @property
    def db(self):
        return self.bot.db  # type: ignore[attr-defined]

    group = app_commands.Group(
        name="server",
        description="🔐 Minecraft server allow-list for the redemption API.",
    )

    @group.command(
        name="setip",
        description="[Admin] Register the Minecraft server IP allowed to redeem shop keys.",
    )
    @app_commands.describe(ip="The server's public numeric IP (IPv4 or IPv6).")
    @admin_only()
    async def setip(self, interaction: discord.Interaction, ip: str):
        if serverip.looks_like_hostname(ip):
            return await interaction.response.send_message(
                embed=embeds.error(
                    "Enter a Numeric IP",
                    f"`{ip}` looks like a hostname. Enter the server's **public IP "
                    "address** (for example `203.0.113.7`) — the API compares it "
                    "against the address the Forge mod connects from.",
                ),
                ephemeral=True,
            )
        normalised = serverip.normalise(ip)
        if not serverip.is_valid(normalised):
            return await interaction.response.send_message(
                embed=embeds.error(
                    "Invalid IP",
                    f"`{ip}` is not a valid IPv4/IPv6 address.",
                ),
                ephemeral=True,
            )
        await self.db.set_server_ip(interaction.guild_id, normalised)
        await self.db.log_tx(
            guild_id=interaction.guild_id,
            kind="config",
            actor_id=interaction.user.id,
            note=f"Minecraft server IP set to {normalised}",
        )
        return await interaction.response.send_message(
            embed=embeds.success(
                "Server Registered",
                f"The Forge mod may now redeem shop keys **only** from `{normalised}`. "
                "Any request from another address is refused and the order stays unclaimed.",
            ),
            ephemeral=True,
        )

    @group.command(
        name="show",
        description="Show the registered Minecraft server IP and whether redemption is enabled.",
    )
    async def show(self, interaction: discord.Interaction):
        settings = await self.db.get_settings(interaction.guild_id)
        configured = (settings["mc_server_ip"] or "").strip() if settings else ""
        if configured:
            description = (
                f"Allowed server IP: `{configured}`\n"
                "The Forge mod can redeem orders from this address only."
            )
        else:
            description = (
                "**No server IP is registered.** The Forge mod cannot claim any "
                "order until an admin runs `/server setip`."
            )
        return await interaction.response.send_message(
            embed=embeds.info("Redemption Server", description), ephemeral=True
        )

    @group.command(
        name="clear",
        description="[Admin] Remove the server IP and disable in-game redemption.",
    )
    @admin_only()
    async def clear(self, interaction: discord.Interaction):
        await self.db.set_server_ip(interaction.guild_id, None)
        await self.db.log_tx(
            guild_id=interaction.guild_id,
            kind="config",
            actor_id=interaction.user.id,
            note="Minecraft server IP cleared",
        )
        return await interaction.response.send_message(
            embed=embeds.success(
                "Server Cleared",
                "No server IP is registered; in-game redemption is disabled until "
                "an admin sets one again.",
            ),
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Server(bot))
