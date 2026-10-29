"""
cogs/nation.py
==============
Nation management commands.

A nation is owned by exactly ONE person (its leader). Admins create nations,
assign the single leader, and set the in-game stats that drive payouts.
"""

from __future__ import annotations

import sqlite3
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import embeds
from checks import admin_only
# BUGFIX: this import was missing, so /nation request raised NameError at runtime.
from creation import creation_bonus, found_account, submit_creation_request
from payouts import nation_payout
from utils import nation_autocomplete, money


class Nation(commands.Cog):
    """🏛️ Manage the nations of the realm."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @property
    def db(self):
        return self.bot.db  # type: ignore[attr-defined]

    group = app_commands.Group(name="nation", description="🏛️ Manage nations of the realm.")

    # -- request (petition; needs admin approval) --------------------------
    @group.command(name="request", description="Petition to found a nation (requires admin approval).")
    @app_commands.describe(name="The nation's name.", leader="The single person who will lead this nation.")
    async def request(self, interaction: discord.Interaction, name: str, leader: discord.Member):
        await submit_creation_request(self.bot, interaction, "nation", name, leader)

    # -- delete ------------------------------------------------------------
    @group.command(name="delete", description="[Admin] Disband a nation.")
    @app_commands.describe(nation="The nation to disband.")
    @app_commands.autocomplete(nation=nation_autocomplete)
    @admin_only()
    async def delete(self, interaction: discord.Interaction, nation: str):
        row = await self.db.get_nation(interaction.guild_id, nation)
        if not row:
            return await interaction.response.send_message(
                embed=embeds.error("Not Found", f"No nation named **{nation}**."), ephemeral=True
            )
        burned = await self.db.delete_account(
            "nation",
            row["id"],
            actor_id=interaction.user.id,
            guild_id=interaction.guild_id,
            note=f"Nation {row['name']} disbanded",
        )
        body = f"🏛️ **{row['name']}** has been disbanded."
        if burned:
            body += (
                f"\nRemaining **{money(burned)}** were burned and logged to the ledger."
            )
        await interaction.response.send_message(
            embed=embeds.success("Nation Disbanded", body)
        )

    # -- add ---------------------------------------------------------------
    @group.command(name="add", description="[Admin] Found a nation immediately, without a petition.")
    @app_commands.describe(name="The nation's name.", leader="The person who will lead it (optional).")
    @admin_only()
    async def add(self, interaction: discord.Interaction, name: str, leader: Optional[discord.Member] = None):
        name = name.strip()
        if not name:
            return await interaction.response.send_message(
                embed=embeds.error("Invalid Name", "A nation needs a name."), ephemeral=True
            )
        if len(name) > 100:
            return await interaction.response.send_message(
                embed=embeds.error("Invalid Name", "Names are limited to 100 characters."), ephemeral=True
            )
        if await self.db.get_nation(interaction.guild_id, name):
            return await interaction.response.send_message(
                embed=embeds.error("Name Taken", f"A nation named **{name}** already exists."), ephemeral=True
            )
        for p in await self.db.pending_creations_for_guild(interaction.guild_id):
            if p["kind"] == "nation" and p["name"].lower() == name.lower():
                return await interaction.response.send_message(
                    embed=embeds.error(
                        "Petition Pending",
                        f"**{name}** already has a pending petition. Approve or deny it instead.",
                    ),
                    ephemeral=True,
                )

        bonus = creation_bonus("nation")
        try:
            _, after = await found_account(
                self.db, interaction.guild_id, "nation", name,
                leader.id if leader else None, interaction.user.id,
                note="Founded directly by an administrator",
            )
        except sqlite3.IntegrityError:
            # A petition approval can win the name race between the check
            # above and the insert. Its status flip and our insert share the
            # database lock, so this is the same one-row outcome.
            return await interaction.response.send_message(
                embed=embeds.error("Name Taken", f"A nation named **{name}** already exists."),
                ephemeral=True,
            )
        await interaction.response.send_message(
            embed=embeds.success(
                "Nation Founded",
                f"🏛️ **{name}** has been founded.\n"
                f"Leader: {leader.mention if leader else '_unassigned_'}\n"
                f"Creation bonus credited: **{money(bonus)}** → Royal Bank balance **{money(after)}**",
            )
        )

    # -- rename ------------------------------------------------------------
    @group.command(name="rename", description="[Admin] Rename a nation, keeping its balance and history.")
    @app_commands.describe(nation="The nation to rename.", new_name="The new name.")
    @app_commands.autocomplete(nation=nation_autocomplete)
    @admin_only()
    async def rename(self, interaction: discord.Interaction, nation: str, new_name: str):
        row = await self.db.get_nation(interaction.guild_id, nation)
        if not row:
            return await interaction.response.send_message(
                embed=embeds.error("Not Found", f"No nation named **{nation}**."), ephemeral=True
            )
        new_name = new_name.strip()
        if not new_name or len(new_name) > 100:
            return await interaction.response.send_message(
                embed=embeds.error("Invalid Name", "Names must be 1–100 characters."), ephemeral=True
            )
        if new_name == row["name"]:
            return await interaction.response.send_message(
                embed=embeds.info("No Change", f"That nation is already called **{new_name}**."),
                ephemeral=True,
            )
        clash = await self.db.get_nation(interaction.guild_id, new_name)
        if clash and clash["id"] != row["id"]:
            return await interaction.response.send_message(
                embed=embeds.error("Name Taken", f"A nation named **{new_name}** already exists."),
                ephemeral=True,
            )
        for p in await self.db.pending_creations_for_guild(interaction.guild_id):
            if p["kind"] == "nation" and p["name"].lower() == new_name.lower():
                return await interaction.response.send_message(
                    embed=embeds.error(
                        "Petition Pending",
                        f"**{new_name}** already has a pending petition. Resolve it before reusing the name.",
                    ),
                    ephemeral=True,
                )

        async with self.db.transaction():
            await self.db.rename_account("nation", row["id"], new_name)
            await self.db.log_tx(
                guild_id=interaction.guild_id, kind="nation_rename",
                account_type="nation", account_id=row["id"], account_name=new_name,
                balance_after=int(row["balance"]), actor_id=interaction.user.id,
                note=f"Renamed from {row['name']}",
            )
        await interaction.response.send_message(
            embed=embeds.success(
                "Nation Renamed",
                f"🏛️ **{row['name']}** is now **{new_name}**.\n"
                "Its balance, leader, contracts and Union membership are unchanged.",
            )
        )

    # -- setleader ---------------------------------------------------------
    @group.command(name="setleader", description="[Admin] Assign the single leader of a nation.")
    @app_commands.describe(nation="The nation.", leader="The person who will lead it.")
    @app_commands.autocomplete(nation=nation_autocomplete)
    @admin_only()
    async def setleader(self, interaction: discord.Interaction, nation: str, leader: discord.Member):
        row = await self.db.get_nation(interaction.guild_id, nation)
        if not row:
            return await interaction.response.send_message(
                embed=embeds.error("Not Found", f"No nation named **{nation}**."), ephemeral=True
            )
        await self.db.set_nation_leader(row["id"], leader.id)
        await self.db.log_tx(
            guild_id=interaction.guild_id, kind="nation_setleader",
            account_type="nation", account_id=row["id"], account_name=row["name"],
            actor_id=interaction.user.id, note=f"Leader -> {leader}",
        )
        await interaction.response.send_message(
            embed=embeds.success("Leader Assigned", f"👑 {leader.mention} now leads **{row['name']}**.")
        )

    # -- stats -------------------------------------------------------------
    @group.command(name="stats", description="[Admin] Set a nation's in-game worker & soldier counts.")
    @app_commands.describe(
        nation="The nation.",
        happy_workers="Number of happy workers.",
        regular_workers="Number of regular workers.",
        happy_soldiers="Number of happy soldiers.",
        regular_soldiers="Number of regular soldiers.",
    )
    @app_commands.autocomplete(nation=nation_autocomplete)
    @admin_only()
    async def stats(
        self, interaction: discord.Interaction, nation: str,
        happy_workers: int, regular_workers: int, happy_soldiers: int, regular_soldiers: int,
    ):
        row = await self.db.get_nation(interaction.guild_id, nation)
        if not row:
            return await interaction.response.send_message(
                embed=embeds.error("Not Found", f"No nation named **{nation}**."), ephemeral=True
            )
        if min(happy_workers, regular_workers, happy_soldiers, regular_soldiers) < 0:
            return await interaction.response.send_message(
                embed=embeds.error("Invalid", "Counts cannot be negative."), ephemeral=True
            )
        await self.db.update_nation_stats(
            row["id"], happy_workers, regular_workers, happy_soldiers, regular_soldiers
        )
        await self.db.log_tx(
            guild_id=interaction.guild_id, kind="nation_stats",
            account_type="nation", account_id=row["id"], account_name=row["name"],
            actor_id=interaction.user.id,
            note=(f"HW={happy_workers} RW={regular_workers} "
                  f"HS={happy_soldiers} RS={regular_soldiers}"),
        )
        result = nation_payout(happy_workers, regular_workers, happy_soldiers, regular_soldiers)
        await interaction.response.send_message(
            embed=embeds.payout_embed(f"Stats Updated · {row['name']}", result)
        )

    # -- info --------------------------------------------------------------
    @group.command(name="info", description="View a nation's stats, balance and next payout.")
    @app_commands.describe(nation="The nation to inspect.")
    @app_commands.autocomplete(nation=nation_autocomplete)
    async def info(self, interaction: discord.Interaction, nation: str):
        row = await self.db.get_nation(interaction.guild_id, nation)
        if not row:
            return await interaction.response.send_message(
                embed=embeds.error("Not Found", f"No nation named **{nation}**."), ephemeral=True
            )
        result = nation_payout(
            row["happy_workers"], row["regular_workers"], row["happy_soldiers"], row["regular_soldiers"]
        )
        embed = embeds.account_embed("nation", row, next_payout=result.total)
        if row["kingdom_id"]:
            kingdom = await self.db.get_kingdom_by_id(row["kingdom_id"])
            if kingdom:
                embed.add_field(name="Member of Kingdom", value=f"👑 {kingdom['name']}", inline=True)
        await interaction.response.send_message(embed=embed)

    # -- list --------------------------------------------------------------
    @group.command(name="list", description="List all nations of the realm.")
    async def list_nations(self, interaction: discord.Interaction):
        rows = await self.db.list_nations(interaction.guild_id)
        if not rows:
            return await interaction.response.send_message(
                embed=embeds.info("No Nations", "No nations have been founded yet."), ephemeral=True
            )
        lines = []
        for n in rows:
            leader = f"<@{n['leader_id']}>" if n["leader_id"] else "—"
            lines.append(f"🏛️ **{n['name']}** — {leader} · Bank: {money(n['balance'])}")
        embed = embeds.info(f"Nations of the Realm ({len(rows)})", "\n".join(lines))
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Nation(bot))
