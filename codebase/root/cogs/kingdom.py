"""
cogs/kingdom.py
===============
Kingdom management commands.

A kingdom is a separate system from a nation. It is composed of up to
SIX member nations and has its own treasury (Royal Bank) and leader.
"""

from __future__ import annotations

import sqlite3
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import config
import embeds
from checks import admin_only
from creation import creation_bonus, found_account, submit_creation_request
from payouts import kingdom_payout
from utils import kingdom_autocomplete, nation_autocomplete, money


class Kingdom(commands.Cog):
    """👑 Manage the kingdoms of the realm."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @property
    def db(self):
        return self.bot.db  # type: ignore[attr-defined]

    group = app_commands.Group(name="kingdom", description="👑 Manage kingdoms of the realm.")

    # -- request (petition; needs admin approval) --------------------------
    @group.command(name="request", description="Petition to found a kingdom (requires admin approval).")
    @app_commands.describe(name="The kingdom's name.", leader="The person who will lead this kingdom.")
    async def request(self, interaction: discord.Interaction, name: str, leader: discord.Member):
        await submit_creation_request(self.bot, interaction, "kingdom", name, leader)

    # -- delete ------------------------------------------------------------
    @group.command(name="delete", description="[Admin] Disband a kingdom (member nations are released).")
    @app_commands.describe(kingdom="The kingdom to disband.")
    @app_commands.autocomplete(kingdom=kingdom_autocomplete)
    @admin_only()
    async def delete(self, interaction: discord.Interaction, kingdom: str):
        row = await self.db.get_kingdom(interaction.guild_id, kingdom)
        if not row:
            return await interaction.response.send_message(
                embed=embeds.error("Not Found", f"No kingdom named **{kingdom}**."), ephemeral=True
            )
        burned = await self.db.delete_account(
            "kingdom",
            row["id"],
            actor_id=interaction.user.id,
            guild_id=interaction.guild_id,
            note=f"Kingdom {row['name']} disbanded",
        )
        body = (
            f"👑 **{row['name']}** has been disbanded. Its nations are free."
        )
        if burned:
            body += (
                f"\nRemaining **{money(burned)}** were burned and logged to the ledger."
            )
        await interaction.response.send_message(
            embed=embeds.success("Kingdom Disbanded", body)
        )

    # -- add ---------------------------------------------------------------
    @group.command(name="add", description="[Admin] Found a kingdom immediately, without a petition.")
    @app_commands.describe(name="The kingdom's name.", leader="The person who will lead it (optional).")
    @admin_only()
    async def add(self, interaction: discord.Interaction, name: str, leader: Optional[discord.Member] = None):
        name = name.strip()
        if not name:
            return await interaction.response.send_message(
                embed=embeds.error("Invalid Name", "A kingdom needs a name."), ephemeral=True
            )
        if len(name) > 100:
            return await interaction.response.send_message(
                embed=embeds.error("Invalid Name", "Names are limited to 100 characters."), ephemeral=True
            )
        if await self.db.get_kingdom(interaction.guild_id, name):
            return await interaction.response.send_message(
                embed=embeds.error("Name Taken", f"A kingdom named **{name}** already exists."), ephemeral=True
            )
        for p in await self.db.pending_creations_for_guild(interaction.guild_id):
            if p["kind"] == "kingdom" and p["name"].lower() == name.lower():
                return await interaction.response.send_message(
                    embed=embeds.error(
                        "Petition Pending",
                        f"**{name}** already has a pending petition. Approve or deny it instead.",
                    ),
                    ephemeral=True,
                )

        bonus = creation_bonus("kingdom")
        try:
            _, after = await found_account(
                self.db, interaction.guild_id, "kingdom", name,
                leader.id if leader else None, interaction.user.id,
                note="Founded directly by an administrator",
            )
        except sqlite3.IntegrityError:
            # A petition approval can win the name race between the check
            # above and the insert. Its status flip and our insert share the
            # database lock, so this is the same one-row outcome.
            return await interaction.response.send_message(
                embed=embeds.error("Name Taken", f"A kingdom named **{name}** already exists."),
                ephemeral=True,
            )
        await interaction.response.send_message(
            embed=embeds.success(
                "Kingdom Founded",
                f"👑 **{name}** has been founded.\n"
                f"Leader: {leader.mention if leader else '_unassigned_'}\n"
                f"Creation bonus credited: **{money(bonus)}** → Royal Bank balance **{money(after)}**",
            )
        )

    # -- rename ------------------------------------------------------------
    @group.command(name="rename", description="[Admin] Rename a kingdom, keeping its balance and history.")
    @app_commands.describe(kingdom="The kingdom to rename.", new_name="The new name.")
    @app_commands.autocomplete(kingdom=kingdom_autocomplete)
    @admin_only()
    async def rename(self, interaction: discord.Interaction, kingdom: str, new_name: str):
        row = await self.db.get_kingdom(interaction.guild_id, kingdom)
        if not row:
            return await interaction.response.send_message(
                embed=embeds.error("Not Found", f"No kingdom named **{kingdom}**."), ephemeral=True
            )
        new_name = new_name.strip()
        if not new_name or len(new_name) > 100:
            return await interaction.response.send_message(
                embed=embeds.error("Invalid Name", "Names must be 1–100 characters."), ephemeral=True
            )
        if new_name == row["name"]:
            return await interaction.response.send_message(
                embed=embeds.info("No Change", f"That kingdom is already called **{new_name}**."),
                ephemeral=True,
            )
        clash = await self.db.get_kingdom(interaction.guild_id, new_name)
        if clash and clash["id"] != row["id"]:
            return await interaction.response.send_message(
                embed=embeds.error("Name Taken", f"A kingdom named **{new_name}** already exists."),
                ephemeral=True,
            )
        for p in await self.db.pending_creations_for_guild(interaction.guild_id):
            if p["kind"] == "kingdom" and p["name"].lower() == new_name.lower():
                return await interaction.response.send_message(
                    embed=embeds.error(
                        "Petition Pending",
                        f"**{new_name}** already has a pending petition. Resolve it before reusing the name.",
                    ),
                    ephemeral=True,
                )

        async with self.db.transaction():
            await self.db.rename_account("kingdom", row["id"], new_name)
            await self.db.log_tx(
                guild_id=interaction.guild_id, kind="kingdom_rename",
                account_type="kingdom", account_id=row["id"], account_name=new_name,
                balance_after=int(row["balance"]), actor_id=interaction.user.id,
                note=f"Renamed from {row['name']}",
            )
        await interaction.response.send_message(
            embed=embeds.success(
                "Kingdom Renamed",
                f"👑 **{row['name']}** is now **{new_name}**.\n"
                "Its balance, leader, member nations, contracts and Union membership are unchanged.",
            )
        )

    # -- setleader ---------------------------------------------------------
    @group.command(name="setleader", description="[Admin] Assign the leader of a kingdom.")
    @app_commands.describe(kingdom="The kingdom.", leader="The person who will lead it.")
    @app_commands.autocomplete(kingdom=kingdom_autocomplete)
    @admin_only()
    async def setleader(self, interaction: discord.Interaction, kingdom: str, leader: discord.Member):
        row = await self.db.get_kingdom(interaction.guild_id, kingdom)
        if not row:
            return await interaction.response.send_message(
                embed=embeds.error("Not Found", f"No kingdom named **{kingdom}**."), ephemeral=True
            )
        await self.db.set_kingdom_leader(row["id"], leader.id)
        await self.db.log_tx(
            guild_id=interaction.guild_id, kind="kingdom_setleader",
            account_type="kingdom", account_id=row["id"], account_name=row["name"],
            actor_id=interaction.user.id, note=f"Leader -> {leader}",
        )
        await interaction.response.send_message(
            embed=embeds.success("Leader Assigned", f"👑 {leader.mention} now leads **{row['name']}**.")
        )

    # -- addnation ---------------------------------------------------------
    @group.command(name="addnation", description="[Admin] Add a member nation to a kingdom (max 6).")
    @app_commands.describe(kingdom="The kingdom.", nation="The nation to add.")
    @app_commands.autocomplete(kingdom=kingdom_autocomplete, nation=nation_autocomplete)
    @admin_only()
    async def addnation(self, interaction: discord.Interaction, kingdom: str, nation: str):
        krow = await self.db.get_kingdom(interaction.guild_id, kingdom)
        nrow = await self.db.get_nation(interaction.guild_id, nation)
        if not krow:
            return await interaction.response.send_message(
                embed=embeds.error("Not Found", f"No kingdom named **{kingdom}**."), ephemeral=True
            )
        if not nrow:
            return await interaction.response.send_message(
                embed=embeds.error("Not Found", f"No nation named **{nation}**."), ephemeral=True
            )
        members = await self.db.nations_in_kingdom(krow["id"])
        if nrow["kingdom_id"] == krow["id"]:
            return await interaction.response.send_message(
                embed=embeds.error("Already a Member", f"**{nrow['name']}** is already in **{krow['name']}**."),
                ephemeral=True,
            )
        if len(members) >= config.MAX_NATIONS_PER_KINGDOM:
            return await interaction.response.send_message(
                embed=embeds.error(
                    "Kingdom Full",
                    f"**{krow['name']}** already has the maximum of "
                    f"**{config.MAX_NATIONS_PER_KINGDOM}** member nations.",
                ),
                ephemeral=True,
            )
        if nrow["kingdom_id"] is not None:
            old = await self.db.get_kingdom_by_id(nrow["kingdom_id"])
            old_name = old["name"] if old else "another kingdom"
            await self.db.set_nation_kingdom(nrow["id"], krow["id"])
            await self.db.log_tx(
                guild_id=interaction.guild_id, kind="kingdom_addnation",
                account_type="kingdom", account_id=krow["id"], account_name=krow["name"],
                counterparty_type="nation", counterparty_id=nrow["id"], counterparty_name=nrow["name"],
                actor_id=interaction.user.id, note=f"Moved from {old_name}",
            )
            return await interaction.response.send_message(
                embed=embeds.success(
                    "Nation Reassigned",
                    f"🏛️ **{nrow['name']}** moved from **{old_name}** to 👑 **{krow['name']}**.",
                )
            )
        await self.db.set_nation_kingdom(nrow["id"], krow["id"])
        await self.db.log_tx(
            guild_id=interaction.guild_id, kind="kingdom_addnation",
            account_type="kingdom", account_id=krow["id"], account_name=krow["name"],
            counterparty_type="nation", counterparty_id=nrow["id"], counterparty_name=nrow["name"],
            actor_id=interaction.user.id,
        )
        await interaction.response.send_message(
            embed=embeds.success(
                "Nation Joined",
                f"🏛️ **{nrow['name']}** joined 👑 **{krow['name']}** "
                f"({len(members) + 1}/{config.MAX_NATIONS_PER_KINGDOM}).",
            )
        )

    # -- removenation ------------------------------------------------------
    @group.command(name="removenation", description="[Admin] Remove a member nation from a kingdom.")
    @app_commands.describe(kingdom="The kingdom.", nation="The nation to remove.")
    @app_commands.autocomplete(kingdom=kingdom_autocomplete, nation=nation_autocomplete)
    @admin_only()
    async def removenation(self, interaction: discord.Interaction, kingdom: str, nation: str):
        krow = await self.db.get_kingdom(interaction.guild_id, kingdom)
        nrow = await self.db.get_nation(interaction.guild_id, nation)
        if not krow or not nrow:
            return await interaction.response.send_message(
                embed=embeds.error("Not Found", "Could not find that kingdom or nation."), ephemeral=True
            )
        if nrow["kingdom_id"] != krow["id"]:
            return await interaction.response.send_message(
                embed=embeds.error("Not a Member", f"**{nrow['name']}** is not part of **{krow['name']}**."),
                ephemeral=True,
            )
        await self.db.set_nation_kingdom(nrow["id"], None)
        await self.db.log_tx(
            guild_id=interaction.guild_id, kind="kingdom_removenation",
            account_type="kingdom", account_id=krow["id"], account_name=krow["name"],
            counterparty_type="nation", counterparty_id=nrow["id"], counterparty_name=nrow["name"],
            actor_id=interaction.user.id,
        )
        await interaction.response.send_message(
            embed=embeds.success("Nation Removed", f"🏛️ **{nrow['name']}** left 👑 **{krow['name']}**.")
        )

    # -- info --------------------------------------------------------------
    @group.command(name="info", description="View a kingdom's members, balance and next payout.")
    @app_commands.describe(kingdom="The kingdom to inspect.")
    @app_commands.autocomplete(kingdom=kingdom_autocomplete)
    async def info(self, interaction: discord.Interaction, kingdom: str):
        krow = await self.db.get_kingdom(interaction.guild_id, kingdom)
        if not krow:
            return await interaction.response.send_message(
                embed=embeds.error("Not Found", f"No kingdom named **{kingdom}**."), ephemeral=True
            )
        members = await self.db.nations_in_kingdom(krow["id"])
        result = kingdom_payout(members)
        embed = embeds.account_embed("kingdom", krow, next_payout=result.total, member_count=len(members))
        if members:
            embed.add_field(
                name="Member Nations",
                value="\n".join(f"🏛️ {m['name']}" for m in members),
                inline=False,
            )
        else:
            embed.add_field(name="Member Nations", value="_None yet._", inline=False)
        await interaction.response.send_message(embed=embed)

    # -- list --------------------------------------------------------------
    @group.command(name="list", description="List all kingdoms of the realm.")
    async def list_kingdoms(self, interaction: discord.Interaction):
        rows = await self.db.list_kingdoms(interaction.guild_id)
        if not rows:
            return await interaction.response.send_message(
                embed=embeds.info("No Kingdoms", "No kingdoms have been founded yet."), ephemeral=True
            )
        # One grouped query instead of one query per kingdom (avoids N+1).
        counts = await self.db.kingdom_member_counts(interaction.guild_id)
        lines = []
        for k in rows:
            leader = f"<@{k['leader_id']}>" if k["leader_id"] else "—"
            lines.append(
                f"👑 **{k['name']}** — {leader} · {counts.get(k['id'], 0)} nation(s) · Bank: {money(k['balance'])}"
            )
        embed = embeds.info(f"Kingdoms of the Realm ({len(rows)})", "\n".join(lines))
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Kingdom(bot))
