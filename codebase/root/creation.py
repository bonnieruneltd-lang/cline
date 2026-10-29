"""
creation.py
===========
Nation & Kingdom *creation approval* flow.

Creation is no longer instant: a player (or admin) submits a request, which
is posted to the **approvals channel** with Approve / Deny buttons. Only an
admin may decide. On approval the account is founded and its one-time
creation bonus is credited to the Royal Bank.

DM policy (per the user's decision): the bot only DMs **kingdom leaders**
about approvals — nation requesters are never DM'd.
"""

from __future__ import annotations

import sqlite3

import discord

import config
import embeds
from utils import money


def creation_bonus(kind: str) -> int:
    return config.NATION_CREATION_BONUS if kind == "nation" else config.KINGDOM_CREATION_BONUS


async def found_account(
    db,
    guild_id: int,
    kind: str,
    name: str,
    leader_id,
    actor_id,
    *,
    note: str = "",
) -> tuple[int, int]:
    """Create an account, credit its one-time bonus and write the ledger row.

    The caller may already hold a transaction; ``Database.transaction`` is
    re-entrant, so approval can join this work to the petition status flip.
    """
    table = "nations" if kind == "nation" else "kingdoms"
    bonus = creation_bonus(kind)
    async with db.transaction():
        if kind == "nation":
            account_id = await db.create_nation(guild_id, name, leader_id)
        else:
            account_id = await db.create_kingdom(guild_id, name, leader_id)
        after = await db.add_balance(table, account_id, bonus)
        await db.log_tx(
            guild_id=guild_id,
            kind=f"{kind}_create",
            account_type=kind,
            account_id=account_id,
            account_name=name,
            amount=bonus,
            balance_after=after,
            actor_id=actor_id,
            note=note or f"Creation bonus {bonus}",
        )
    return account_id, after


def _label(kind: str) -> str:
    return "Nation" if kind == "nation" else "Kingdom"


def _emoji(kind: str) -> str:
    return "🏛️" if kind == "nation" else "👑"


def build_request_embed(row) -> discord.Embed:
    kind = row["kind"]
    leader = f"<@{row['leader_id']}>" if row["leader_id"] else "_unassigned_"
    embed = discord.Embed(
        title=f"📜 Petition · New {_label(kind)}",
        description=(
            f"**{_emoji(kind)} {row['name']}** has petitioned to be founded.\n\n"
            f"**Proposed Leader:** {leader}"
        ),
        color=config.GOLD_COLOR,
    )
    embed.add_field(name="Requested By", value=f"<@{row['requested_by']}>", inline=True)
    embed.add_field(name="Creation Bonus", value=f"**{money(creation_bonus(kind))}**", inline=True)
    embed.set_footer(text=f"⚜ Royal Court · Petition #{row['id']} · awaiting an administrator")
    return embed


async def submit_creation_request(
    bot, interaction: discord.Interaction, kind: str, name: str, leader: discord.Member
) -> None:
    """Validate, store, and post a creation petition to the approvals channel."""
    db = bot.db

    if interaction.guild_id is None:
        return await interaction.response.send_message(
            embed=embeds.error("Guild Only", "Petitions only work inside a server."),
            ephemeral=True,
        )
    name = (name or "").strip()
    if not name or len(name) > 100:
        return await interaction.response.send_message(
            embed=embeds.error("Invalid Name", "Names must be 1–100 characters."),
            ephemeral=True,
        )

    # Name uniqueness (case-insensitive) against existing accounts.
    if kind == "nation":
        if await db.get_nation(interaction.guild_id, name):
            return await interaction.response.send_message(
                embed=embeds.error("Name Taken", f"A nation named **{name}** already exists."), ephemeral=True
            )
    else:
        if await db.get_kingdom(interaction.guild_id, name):
            return await interaction.response.send_message(
                embed=embeds.error("Name Taken", f"A kingdom named **{name}** already exists."), ephemeral=True
            )

    # No duplicate pending petitions with the same name.
    for p in await db.pending_creations_for_guild(interaction.guild_id):
        if p["kind"] == kind and p["name"].lower() == name.lower():
            return await interaction.response.send_message(
                embed=embeds.error("Already Pending", f"A petition for **{name}** is already awaiting approval."),
                ephemeral=True,
            )

    settings = await db.get_settings(interaction.guild_id)
    channel_id = settings["approval_channel_id"]
    if not channel_id:
        return await interaction.response.send_message(
            embed=embeds.error(
                "No Approvals Channel",
                "An administrator must first set the approvals channel with "
                "`/decree setchannel type:Approvals`.",
            ),
            ephemeral=True,
        )
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except Exception:
            channel = None
    if channel is None:
        return await interaction.response.send_message(
            embed=embeds.error("Channel Missing", "The configured approvals channel could not be found."),
            ephemeral=True,
        )

    creation_id = await db.create_pending_creation(
        interaction.guild_id, kind, name, leader.id if leader else None, interaction.user.id
    )
    row = await db.get_pending_creation(creation_id)
    view = CreationApprovalView(bot, creation_id)
    bot.add_view(view)

    await interaction.response.send_message(
        embed=embeds.info("Submitting Petition", "Posting the petition for administrator review…"),
        ephemeral=True,
    )
    try:
        msg = await channel.send(embed=build_request_embed(row), view=view)
        await db.set_creation_message(creation_id, channel.id, msg.id)
    except discord.HTTPException:
        # Forbidden, NotFound and transient Discord errors all leave the same
        # dead row; none may stay pending with no message to click.
        async with db.transaction():
            await db.claim_pending_creation(creation_id, "failed")
        return await interaction.edit_original_response(
            embed=embeds.error("Cannot Post", "I could not post in the approvals channel.")
        )

    await db.log_tx(
        guild_id=interaction.guild_id, kind=f"{kind}_request",
        account_type=kind, account_name=name,
        actor_id=interaction.user.id,
        note=f"Petition #{creation_id} submitted (leader: {leader})",
    )
    await interaction.edit_original_response(
        embed=embeds.success(
            "Petition Submitted",
            f"Your petition to found {_emoji(kind)} **{name}** was sent to the administrators.\n"
            f"Once approved, it will be founded with a **{money(creation_bonus(kind))}** creation bonus.",
        )
    )


class CreationApprovalView(discord.ui.View):
    """Persistent Approve/Deny buttons for a creation petition."""

    def __init__(self, bot, creation_id: int) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.creation_id = creation_id

        approve = discord.ui.Button(
            label="Approve", style=discord.ButtonStyle.success, emoji="✅",
            custom_id=f"creation:approve:{creation_id}",
        )
        deny = discord.ui.Button(
            label="Deny", style=discord.ButtonStyle.danger, emoji="⛔",
            custom_id=f"creation:deny:{creation_id}",
        )
        approve.callback = self._approve
        deny.callback = self._deny
        self.add_item(approve)
        self.add_item(deny)

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _is_admin(interaction: discord.Interaction) -> bool:
        perms = getattr(interaction.user, "guild_permissions", None)
        return bool(perms and perms.administrator)

    async def _notify_kingdom_leader(self, user_id, embed: discord.Embed) -> None:
        """DM policy: only kingdom leaders are DM'd about approvals."""
        try:
            user = await self.bot.fetch_user(user_id)
            await user.send(embed=embed)
        except Exception:
            pass

    @staticmethod
    async def _safe_edit(interaction: discord.Interaction, embed: discord.Embed) -> None:
        """Edit the petition message, falling back to a followup if it's gone."""
        try:
            await interaction.response.edit_message(embed=embed, view=None)
        except discord.HTTPException:
            try:
                await interaction.followup.send(embed=embed, ephemeral=True)
            except discord.HTTPException:
                pass

    # -- callbacks ---------------------------------------------------------
    async def _approve(self, interaction: discord.Interaction) -> None:
        db = self.bot.db
        row = await db.get_pending_creation(self.creation_id)
        if not row or row["status"] != "pending":
            return await interaction.response.send_message(
                embed=embeds.error("Already Handled", "This petition has already been resolved."), ephemeral=True
            )
        if not self._is_admin(interaction):
            return await interaction.response.send_message(
                embed=embeds.error("Not Authorized", "Only an administrator may approve petitions."), ephemeral=True
            )

        kind = row["kind"]
        name = row["name"]
        leader_id = row["leader_id"]
        bonus = creation_bonus(kind)

        # Re-check uniqueness at approval time.
        if kind == "nation":
            existing = await db.get_nation(row["guild_id"], name)
        else:
            existing = await db.get_kingdom(row["guild_id"], name)
        if existing:
            async with db.transaction():
                marked = await db.claim_pending_creation(self.creation_id, "failed")
            if not marked:
                return await interaction.response.send_message(
                    embed=embeds.error("Already Handled", "This petition has already been resolved."),
                    ephemeral=True,
                )
            return await self._safe_edit(
                interaction,
                embeds.error("Name Taken", f"A {_label(kind).lower()} named **{name}** already exists."),
            )

        # Founding the account, crediting the bonus and closing the petition is
        # one unit of work: a crash must not leave an account with no bonus, and
        # two administrators clicking at once must found it only once.
        won = False
        try:
            async with db.transaction():
                if await db.claim_pending_creation(self.creation_id, "approved"):
                    won = True
                    account_id, after = await found_account(
                        db, row["guild_id"], kind, name, leader_id, interaction.user.id,
                        note=f"Approved petition #{self.creation_id} · creation bonus {bonus}",
                    )
        except sqlite3.IntegrityError:
            # A direct /nation add or /kingdom add won the name race between our
            # look-up and our insert. The founding transaction rolled back, so
            # close the petition explicitly in a fresh transaction. Leaving it
            # pending would make the message say "Name Taken" while the row
            # stayed claimable forever.
            async with db.transaction():
                marked = await db.claim_pending_creation(self.creation_id, "failed")
            if not marked:
                return await interaction.response.send_message(
                    embed=embeds.error("Already Handled", "This petition has already been resolved."),
                    ephemeral=True,
                )
            return await self._safe_edit(
                interaction,
                embeds.error("Name Taken", f"A {_label(kind).lower()} named **{name}** already exists."),
            )
        if not won:
            return await interaction.response.send_message(
                embed=embeds.error("Already Handled", "This petition has already been resolved."),
                ephemeral=True,
            )

        embed = embeds.success(
            f"{_label(kind)} Founded",
            f"{_emoji(kind)} **{name}** has been founded.\n"
            f"Leader: {f'<@{leader_id}>' if leader_id else '_unassigned_'}\n"
            f"Creation bonus credited: **{money(bonus)}** → Royal Bank balance **{money(after)}**",
        )
        await self._safe_edit(interaction, embed)

        # DM policy: only kingdom leaders get DMs.
        if kind == "kingdom" and leader_id:
            await self._notify_kingdom_leader(
                leader_id,
                embeds.success(
                    "Kingdom Approved",
                    f"👑 **{name}** has been founded and you are its leader.\n"
                    f"A **{money(bonus)}** creation bonus was credited to its Royal Bank.",
                ),
            )

    async def _deny(self, interaction: discord.Interaction) -> None:
        db = self.bot.db
        row = await db.get_pending_creation(self.creation_id)
        if not row or row["status"] != "pending":
            return await interaction.response.send_message(
                embed=embeds.error("Already Handled", "This petition has already been resolved."), ephemeral=True
            )
        if not self._is_admin(interaction):
            return await interaction.response.send_message(
                embed=embeds.error("Not Authorized", "Only an administrator may deny petitions."), ephemeral=True
            )

        won = False
        async with db.transaction():
            won = await db.claim_pending_creation(self.creation_id, "denied")
            if won:
                await db.log_tx(
                    guild_id=row["guild_id"], kind=f"{row['kind']}_request_denied",
                    account_type=row["kind"], account_name=row["name"],
                    actor_id=interaction.user.id, note=f"Petition #{self.creation_id} denied",
                )
        if not won:
            return await interaction.response.send_message(
                embed=embeds.error("Already Handled", "This petition has already been resolved."),
                ephemeral=True,
            )
        embed = embeds.error(
            "Petition Denied",
            f"The petition to found {_emoji(row['kind'])} **{row['name']}** was **denied**.",
        )
        await self._safe_edit(interaction, embed)

        # DM policy: only kingdom leaders get DMs.
        if row["kind"] == "kingdom" and row["leader_id"]:
            await self._notify_kingdom_leader(
                row["leader_id"],
                embeds.error("Kingdom Petition Denied", f"The petition for 👑 **{row['name']}** was denied."),
            )
