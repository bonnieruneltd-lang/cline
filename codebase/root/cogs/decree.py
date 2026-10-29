"""
cogs/decree.py
==============
Royal decrees — server configuration & rulebook reference.

Two channels are configured here:
  * Cycle channel      — where the 14-day payout summaries are announced.
  * Approvals channel  — where nation/kingdom creation petitions go for admins.

Payout RATES and CYCLE LENGTH are fixed for this edition.
"""

from __future__ import annotations

import datetime as dt

import discord
from discord import app_commands
from discord.ext import commands

import config
import embeds
from checks import admin_only
from database import parse_iso
from utils import money


class Decree(commands.Cog):
    """📜 Royal decrees — configuration & rulebook."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @property
    def db(self):
        return self.bot.db  # type: ignore[attr-defined]

    group = app_commands.Group(name="decree", description="📜 Royal decrees — configuration & rulebook.")

    # -- setchannel --------------------------------------------------------
    @group.command(name="setchannel", description="[Admin] Set the cycle channel or the approvals channel.")
    @app_commands.describe(kind="Which channel to configure.", channel="The channel to use.")
    @app_commands.choices(kind=[
        app_commands.Choice(name="Cycle — payout announcements", value="cycle"),
        app_commands.Choice(name="Approvals — creation petitions", value="approvals"),
    ])
    @admin_only()
    async def setchannel(
        self, interaction: discord.Interaction, kind: app_commands.Choice[str], channel: discord.TextChannel
    ):
        if kind.value == "cycle":
            await self.db.set_payout_channel(interaction.guild_id, channel.id)
            settings = await self.db.get_settings(interaction.guild_id)
            if not settings["next_cycle_at"]:
                now = dt.datetime.now(dt.timezone.utc)
                await self.db.set_cycle_times(
                    interaction.guild_id, None,
                    (now + dt.timedelta(days=config.PAYOUT_CYCLE_DAYS)).strftime("%Y-%m-%dT%H:%M:%S"),
                )
            await self.db.log_tx(
                guild_id=interaction.guild_id, kind="config",
                actor_id=interaction.user.id, note=f"Cycle channel set to #{channel.name}",
            )
            return await interaction.response.send_message(
                embed=embeds.success(
                    "Decree Sealed",
                    f"The **cycle** channel is now {channel.mention}. Payout summaries will be posted there.",
                )
            )
        else:
            await self.db.set_approval_channel(interaction.guild_id, channel.id)
            await self.db.log_tx(
                guild_id=interaction.guild_id, kind="config",
                actor_id=interaction.user.id, note=f"Approvals channel set to #{channel.name}",
            )
            return await interaction.response.send_message(
                embed=embeds.success(
                    "Decree Sealed",
                    f"The **approvals** channel is now {channel.mention}. "
                    f"Creation petitions will be posted there for administrators.",
                )
            )

    # -- pending -----------------------------------------------------------
    @group.command(name="pending", description="[Admin] List pending nation/kingdom creation petitions.")
    @admin_only()
    async def pending(self, interaction: discord.Interaction):
        rows = await self.db.pending_creations_for_guild(interaction.guild_id)
        if not rows:
            return await interaction.response.send_message(
                embed=embeds.info("No Petitions", "There are no pending creation petitions."), ephemeral=True
            )
        lines = []
        for p in rows:
            emoji = "🏛️" if p["kind"] == "nation" else "👑"
            leader = f"<@{p['leader_id']}>" if p["leader_id"] else "—"
            lines.append(f"`#{p['id']}` {emoji} **{p['name']}** — leader {leader} · by <@{p['requested_by']}>")
        embed = embeds.info(f"Pending Petitions ({len(rows)})", "\n".join(lines))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # -- rates -------------------------------------------------------------
    @group.command(name="rates", description="Show the current payout rulebook.")
    async def rates(self, interaction: discord.Interaction):
        embed = discord.Embed(title="📜 Royal Court Decrees — Payout Rulebook", color=config.GOLD_COLOR)
        embed.add_field(
            name="🏛️ Nation Payout (every 14 days)",
            value=(
                f"• Base Allowance — **{money(config.NATION_BASE)}**\n"
                f"• Happy Worker — **{money(config.NATION_PER_HAPPY_WORKER)}** each\n"
                f"• Regular Worker — **{money(config.NATION_PER_REGULAR_WORKER)}** each\n"
                f"• Happy Soldier — **{money(config.NATION_PER_HAPPY_SOLDIER)}** each\n"
                f"• Regular Soldier — **{money(config.NATION_PER_REGULAR_SOLDIER)}** each"
            ),
            inline=False,
        )
        embed.add_field(
            name="👑 Kingdom Payout (every 14 days)",
            value=(
                f"• Base Allowance — **{money(config.KINGDOM_BASE)}**\n"
                f"• Member Nation — **{money(config.KINGDOM_PER_MEMBER_NATION)}** each\n"
                f"• Happy Worker — **{money(config.KINGDOM_PER_HAPPY_WORKER)}** each\n"
                f"• Regular Worker — **{money(config.KINGDOM_PER_REGULAR_WORKER)}** each\n"
                f"• Happy Soldier — **{money(config.KINGDOM_PER_HAPPY_SOLDIER)}** each\n"
                f"• Regular Soldier — **{money(config.KINGDOM_PER_REGULAR_SOLDIER)}** each"
            ),
            inline=False,
        )
        embed.add_field(
            name="🎁 One-Time Creation Bonuses",
            value=(
                f"• Founding a Nation — **{money(config.NATION_CREATION_BONUS)}**\n"
                f"• Founding a Kingdom — **{money(config.KINGDOM_CREATION_BONUS)}**"
            ),
            inline=False,
        )
        embed.set_footer(text="⚜ The Royal Court · Royal Bank of the Realm")
        await interaction.response.send_message(embed=embed)

    # -- info --------------------------------------------------------------
    @group.command(name="info", description="Show the payout cycle schedule and configured channels.")
    async def info(self, interaction: discord.Interaction):
        if interaction.guild_id is None:
            return await interaction.response.send_message(
                embed=embeds.error(
                    "Guild Only", "Payout settings belong to a server, not a DM."
                ),
                ephemeral=True,
            )
        settings = await self.db.get_settings(interaction.guild_id)
        cycle = f"<#{settings['payout_channel_id']}>" if settings["payout_channel_id"] else "_not set_"
        approvals = f"<#{settings['approval_channel_id']}>" if settings["approval_channel_id"] else "_not set_"
        last = parse_iso(settings["last_cycle_at"])
        nxt = parse_iso(settings["next_cycle_at"])
        embed = discord.Embed(title="📜 Payout Cycle & Channels", color=config.BLUE_COLOR)
        embed.add_field(name="Cycle Length", value=f"Every **{config.PAYOUT_CYCLE_DAYS}** days", inline=True)
        embed.add_field(name="Cycle Channel", value=cycle, inline=True)
        embed.add_field(name="Approvals Channel", value=approvals, inline=True)
        embed.add_field(
            name="Last Cycle",
            value=f"<t:{int(last.timestamp())}:R>" if last else "_never_",
            inline=True,
        )
        embed.add_field(
            name="Next Cycle",
            value=f"<t:{int(nxt.timestamp())}:F> (<t:{int(nxt.timestamp())}:R>)" if nxt else "_not scheduled_",
            inline=True,
        )
        embed.set_footer(text="⚜ The Royal Court · Royal Bank of the Realm")
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Decree(bot))
