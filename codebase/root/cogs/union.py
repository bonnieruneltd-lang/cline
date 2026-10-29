"""
cogs/union.py
=============
The Workers' Union treasury and membership.

The Union is the only source of contract credits. Its balance grows from
admin funding, member dues and donations, and shrinks only when a contract
tranche is claimed. Nothing else creates Union money.

Accounts that join the Union get a 50% discount on building blocks and food
(not redstone or materials such as iron blocks) and pay dues on every payout:
1.75% of a nation's whole balance, 2% of a kingdom's whole balance.

A whole-balance commitment is separate from membership: after admin approval
in the configured channel, every credit to the account is followed by a
donation of its whole balance to the Union. The
`/faction commitment` command requests one and
`kind:uncommit` stops it.
"""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

import config
import embeds
import factions
from checks import admin_only
from database import InsufficientFunds
from utils import (
    account_autocomplete,
    account_emoji,
    account_type_label,
    is_admin,
    money,
    parse_amount,
    resolve_account,
)


class UnionCommitApprovalView(discord.ui.View):
    """Persistent Approve/Deny buttons for a whole-balance commitment."""

    def __init__(self, bot: commands.Bot, commit_id: int) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.commit_id = commit_id

        approve = discord.ui.Button(
            label="Approve", style=discord.ButtonStyle.success, emoji="✅",
            custom_id=f"unioncommit:approve:{commit_id}",
        )
        deny = discord.ui.Button(
            label="Deny", style=discord.ButtonStyle.danger, emoji="⛔",
            custom_id=f"unioncommit:deny:{commit_id}",
        )
        approve.callback = self._approve
        deny.callback = self._deny
        self.add_item(approve)
        self.add_item(deny)

    @staticmethod
    def _is_admin(interaction: discord.Interaction) -> bool:
        perms = getattr(interaction.user, "guild_permissions", None)
        return bool(perms and perms.administrator)

    async def _notify(self, user_id, embed: discord.Embed) -> None:
        try:
            user = await self.bot.fetch_user(user_id)
            await user.send(embed=embed)
        except Exception:
            pass

    @staticmethod
    async def _safe_edit(interaction: discord.Interaction, embed: discord.Embed) -> None:
        try:
            await interaction.response.edit_message(embed=embed, view=None)
        except discord.HTTPException:
            try:
                await interaction.followup.send(embed=embed, ephemeral=True)
            except discord.HTTPException:
                pass

    async def _finalize(self, interaction: discord.Interaction, approve: bool) -> None:
        db = self.bot.db  # type: ignore[attr-defined]
        row = await db.get_union_commit_by_id(self.commit_id)
        if not row or row["status"] != "pending":
            return await interaction.response.send_message(
                embed=embeds.error("Already Handled", "This request has already been resolved."),
                ephemeral=True,
            )
        if not self._is_admin(interaction):
            return await interaction.response.send_message(
                embed=embeds.error("Not Authorized", "Only an administrator may decide this."),
                ephemeral=True,
            )

        account_type = row["account_type"]
        account_id = int(row["account_id"])
        account = (
            await db.get_kingdom_by_id(account_id)
            if account_type == "kingdom"
            else await db.get_nation_by_id(account_id)
        )
        if account is None:
            async with db.transaction():
                if await db.decide_union_commit(
                    self.commit_id, "denied", interaction.user.id
                ):
                    await db.log_tx(
                        guild_id=row["guild_id"], kind="union_commit_denied",
                        account_type=account_type, account_id=account_id,
                        actor_id=interaction.user.id,
                        note=f"Commit request #{self.commit_id} denied: account no longer exists",
                    )
            return await self._safe_edit(
                interaction,
                embeds.error("Account Gone", "That nation or kingdom no longer exists."),
            )

        if approve:
            member = await db.get_faction_member(
                row["guild_id"], account_type, account_id
            )
            if member is not None and member["faction"] == factions.CROWN:
                async with db.transaction():
                    if await db.decide_union_commit(
                        self.commit_id, "denied", interaction.user.id
                    ):
                        await db.log_tx(
                            guild_id=row["guild_id"],
                            kind="union_commit_denied",
                            account_type=account_type,
                            account_id=account_id,
                            actor_id=interaction.user.id,
                            note=(
                                f"Commit request #{self.commit_id} denied: "
                                "account serves The Crown"
                            ),
                        )
                return await self._safe_edit(
                    interaction,
                    embeds.error(
                        "Crown Allegiance",
                        "That account serves The Crown and cannot make a Union commitment.",
                    ),
                )

        status = "active" if approve else "denied"
        donated = 0
        async with db.transaction():
            if not await db.decide_union_commit(self.commit_id, status, interaction.user.id):
                handled = False
            else:
                handled = True
                await db.log_tx(
                    guild_id=row["guild_id"],
                    kind=f"union_commit_{status}",
                    account_type=account_type, account_id=account_id,
                    account_name=account["name"], actor_id=interaction.user.id,
                    note=(
                        f"Commit request #{self.commit_id} "
                        f"{'approved' if approve else 'denied'}"
                    ),
                )
                if approve:
                    # Approval is the donation of the balance already held; the
                    # rest of the commitment runs on every later credit.
                    donated = await db.commit_balance_to_union(
                        row["guild_id"], account_type, account_id
                    )
        if not handled:
            return await interaction.response.send_message(
                embed=embeds.error("Already Handled", "This request has already been resolved."),
                ephemeral=True,
            )

        emoji = account_emoji(account_type)
        if approve:
            body = (
                f"{emoji} **{account['name']}** now donates its **whole balance** to the "
                f"Union every time it is credited."
            )
            if donated:
                body += f"\n**{money(donated)}** was donated immediately."
            embed = embeds.success("Commitment Approved", body)
        else:
            embed = embeds.error(
                "Commitment Denied",
                f"{emoji} **{account['name']}** will not donate its balance automatically.",
            )
        await self._safe_edit(interaction, embed)

        if row["requested_by"]:
            await self._notify(row["requested_by"], embed)

    async def _approve(self, interaction: discord.Interaction) -> None:
        await self._finalize(interaction, approve=True)

    async def _deny(self, interaction: discord.Interaction) -> None:
        await self._finalize(interaction, approve=False)


class Union(commands.Cog):
    """⚒️ The Workers' Union — treasury, dues and membership."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @property
    def db(self):
        return self.bot.db  # type: ignore[attr-defined]

    group = app_commands.Group(name="union", description="⚒️ The Workers' Union — treasury and membership.")

    # -- helpers -----------------------------------------------------------
    async def _can_act(self, interaction: discord.Interaction, row) -> bool:
        """Admin, or the leader of the account being acted on."""
        if is_admin(interaction):
            return True
        return row["leader_id"] == interaction.user.id

    def _not_authorized(self, account_type: str, row) -> discord.Embed:
        return embeds.error(
            "Not Authorized",
            f"Only the leader of {account_emoji(account_type)} **{row['name']}** "
            f"(or an admin) may do that.",
        )

    # -- balance -----------------------------------------------------------
    @group.command(name="balance", description="Show the Workers' Union treasury and membership rules.")
    async def balance(self, interaction: discord.Interaction):
        if interaction.guild_id is None:
            return await interaction.response.send_message(
                embed=embeds.error("Guild Only", "The Union only runs inside a server."), ephemeral=True
            )
        treasury = await self.db.get_union_balance(interaction.guild_id)
        members = await self.db.list_union_members(interaction.guild_id)
        commitments = await self.db.count_active_union_commits(interaction.guild_id)
        embed = discord.Embed(
            title="⚒️ The Workers' Union · Treasury",
            color=config.GOLD_COLOR,
        )
        embed.add_field(name="Treasury", value=f"## {money(treasury)}", inline=False)
        embed.add_field(
            name="Members",
            value=(
                f"**{len(members)}** account(s) hold membership.\n"
                f"Discount: **{config.UNION_MEMBER_DISCOUNT}% off** "
                f"{', '.join(sorted(config.UNION_MEMBER_DISCOUNT_CATEGORIES))}.\n"
                f"Dues: **{config.union_tax_label('nation')}%** for nations, "
                f"**{config.union_tax_label('kingdom')}%** for kingdoms, "
                f"of the whole balance on every payout.\n"
                f"Commitments: **{commitments}** account(s) donate their whole balance "
                f"automatically (`/faction commitment`)."
            ),
            inline=False,
        )
        embed.set_footer(
            text=(
                "Contracts are funded from this balance · "
                "⚜ The Royal Court · Royal Bank of the Realm"
            )
        )
        await interaction.response.send_message(embed=embed)

    # -- commitment channel ------------------------------------------------
    @group.command(
        name="setchannel",
        description="[Admin] Set the channel where Union commitment requests are approved.",
    )
    @app_commands.describe(channel="The channel admins will watch for commitment requests.")
    @admin_only()
    async def setchannel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await self.db.set_union_commit_channel(interaction.guild_id, channel.id)
        await self.db.log_tx(
            guild_id=interaction.guild_id, kind="config",
            actor_id=interaction.user.id,
            note=f"Union commit channel set to #{channel.name}",
        )
        await interaction.response.send_message(
            embed=embeds.success(
                "Union Channel Set",
                f"Commitment requests will be posted to {channel.mention} for administrator approval.",
            )
        )

    # -- standing commitment -----------------------------------------------
    async def request_commit(self, interaction: discord.Interaction, account: str):
        """Shared Union commitment flow used by /faction commitment."""
        atype, row = await resolve_account(self.db, interaction.guild_id, account)
        if not row:
            return await interaction.response.send_message(
                embed=embeds.error("Not Found", "That account does not exist."), ephemeral=True
            )
        if not await self._can_act(interaction, row):
            return await interaction.response.send_message(
                embed=self._not_authorized(atype, row), ephemeral=True
            )
        member = await self.db.get_faction_member(
            interaction.guild_id, atype, row["id"]
        )
        if member is not None and member["faction"] == factions.CROWN:
            return await interaction.response.send_message(
                embed=embeds.error(
                    "Crown Allegiance",
                    f"{account_emoji(atype)} **{row['name']}** serves **The Crown** "
                    "and cannot make a Union commitment.",
                ),
                ephemeral=True,
            )

        existing = await self.db.get_union_commit(interaction.guild_id, atype, row["id"])
        if existing and existing["status"] == "active":
            return await interaction.response.send_message(
                embed=embeds.info(
                    "Already Committed",
                    f"{account_emoji(atype)} **{row['name']}** already donates its whole "
                    f"balance to the Union on every credit.",
                ),
                ephemeral=True,
            )
        if existing and existing["status"] == "pending":
            return await interaction.response.send_message(
                embed=embeds.info(
                    "Already Pending",
                    f"{account_emoji(atype)} **{row['name']}** already has a commitment "
                    f"request waiting for an administrator.",
                ),
                ephemeral=True,
            )

        settings = await self.db.get_settings(interaction.guild_id)
        channel_id = settings["union_commit_channel_id"]
        if not channel_id:
            return await interaction.response.send_message(
                embed=embeds.error(
                    "No Commit Channel",
                    "An administrator must first set the commitment channel with "
                    "`/faction setchannel`.",
                ),
                ephemeral=True,
            )
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except Exception:
                channel = None
        if channel is None:
            return await interaction.response.send_message(
                embed=embeds.error(
                    "Channel Missing", "The configured commitment channel could not be found."
                ),
                ephemeral=True,
            )

        commit_id = await self.db.create_union_commit_request(
            interaction.guild_id, atype, row["id"], interaction.user.id
        )
        balance_now = int(row["balance"])
        view = UnionCommitApprovalView(self.bot, commit_id)
        self.bot.add_view(view)

        request_embed = discord.Embed(
            title="⚒️ Union Commitment Request",
            description=(
                f"{account_emoji(atype)} **{row['name']}** asks to commit its **whole balance** "
                f"to the Workers' Union.\n\n"
                f"If approved, the balance it holds now is donated immediately, and "
                f"**every future credit** to this account is donated to the Union in full. "
                f"Union dues still apply first on a payout."
            ),
            color=config.GOLD_COLOR,
        )
        request_embed.add_field(name="Requested By", value=f"<@{interaction.user.id}>", inline=True)
        request_embed.add_field(name="Balance Now", value=money(balance_now), inline=True)
        request_embed.set_footer(
            text=(
                f"Request #{commit_id} · administrators only · cancel with "
                "/faction uncommit"
            )
        )
        await interaction.response.send_message(
            embed=embeds.info("Submitting Request", "Posting the commitment request for administrator approval…"),
            ephemeral=True,
        )
        try:
            msg = await channel.send(embed=request_embed, view=view)
            await self.db.set_union_commit_message(commit_id, channel.id, msg.id)
        except discord.HTTPException:
            await self.db.cancel_union_commit(
                interaction.guild_id, atype, row["id"], interaction.user.id
            )
            return await interaction.edit_original_response(
                embed=embeds.error(
                    "Cannot Post", "I could not post in the commitment channel."
                )
            )

        await self.db.log_tx(
            guild_id=interaction.guild_id, kind="union_commit_request",
            account_type=atype, account_id=row["id"], account_name=row["name"],
            actor_id=interaction.user.id,
            note=f"Commit request #{commit_id} submitted",
        )
        await interaction.edit_original_response(
            embed=embeds.success(
                "Commitment Requested",
                f"The request for {account_emoji(atype)} **{row['name']}** was sent to "
                f"the administrators.\n"
                f"Nothing changes until it is approved.",
            )
        )

    async def cancel_commit(self, interaction: discord.Interaction, account: str):
        """Shared cancellation flow used by /faction uncommit."""
        atype, row = await resolve_account(self.db, interaction.guild_id, account)
        if not row:
            return await interaction.response.send_message(
                embed=embeds.error("Not Found", "That account does not exist."), ephemeral=True
            )
        if not await self._can_act(interaction, row):
            return await interaction.response.send_message(
                embed=self._not_authorized(atype, row), ephemeral=True
            )
        if not await self.db.cancel_union_commit(
            interaction.guild_id, atype, row["id"], interaction.user.id
        ):
            return await interaction.response.send_message(
                embed=embeds.info(
                    "Not Committed",
                    f"{account_emoji(atype)} **{row['name']}** has no pending or active "
                    f"commitment.",
                ),
                ephemeral=True,
            )
        await self.db.log_tx(
            guild_id=interaction.guild_id, kind="union_commit_cancelled",
            account_type=atype, account_id=row["id"], account_name=row["name"],
            actor_id=interaction.user.id,
            note="Standing commitment cancelled",
        )
        await interaction.response.send_message(
            embed=embeds.success(
                "Commitment Cancelled",
                f"{account_emoji(atype)} **{row['name']}** no longer donates its balance "
                f"automatically. Credits it receives stay in its Royal Bank.",
            )
        )

    # -- admin funding -----------------------------------------------------
    @group.command(name="fund", description="[Admin] Credit the Union treasury so it can fund contracts.")
    @app_commands.describe(amount="Credits to add to the Union treasury.")
    @admin_only()
    async def fund(self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, None]):
        async with self.db.transaction():
            after = await self.db.credit_union(interaction.guild_id, amount)
            await self.db.log_tx(
                guild_id=interaction.guild_id,
                kind="union_fund",
                account_type="union",
                account_id=interaction.guild_id,
                account_name="Workers' Union",
                amount=amount,
                balance_after=after,
                actor_id=interaction.user.id,
                note="Admin funded the Union treasury",
            )
        await interaction.response.send_message(
            embed=embeds.success(
                "Union Funded",
                f"**{money(amount)}** added to the Union treasury.\n"
                f"New balance: **{money(after)}**",
            )
        )

    # -- donations ---------------------------------------------------------
    @group.command(name="donate", description="Donate credits from a nation or kingdom to the Union.")
    @app_commands.describe(
        account="The nation or kingdom donating.",
        amount="Amount, or all / half / quarter of that account's balance.",
    )
    @app_commands.autocomplete(account=account_autocomplete)
    async def donate(self, interaction: discord.Interaction, account: str, amount: str):
        atype, row = await resolve_account(self.db, interaction.guild_id, account)
        if not row:
            return await interaction.response.send_message(
                embed=embeds.error("Not Found", "That account does not exist."), ephemeral=True
            )
        if not await self._can_act(interaction, row):
            return await interaction.response.send_message(
                embed=self._not_authorized(atype, row), ephemeral=True
            )
        try:
            amount = parse_amount(amount, int(row["balance"]))
        except ValueError as exc:
            return await interaction.response.send_message(
                embed=embeds.error("Invalid Amount", str(exc)), ephemeral=True
            )

        table = "nations" if atype == "nation" else "kingdoms"
        try:
            async with self.db.transaction():
                after = await self.db.debit(table, row["id"], amount)
                union_after = await self.db.credit_union(interaction.guild_id, amount)
                await self.db.log_tx(
                    guild_id=interaction.guild_id,
                    kind="union_donate",
                    account_type=atype,
                    account_id=row["id"],
                    account_name=row["name"],
                    counterparty_type="union",
                    counterparty_name="Workers' Union",
                    amount=-amount,
                    balance_after=after,
                    actor_id=interaction.user.id,
                    note="Donation to the Workers' Union",
                )
                await self.db.log_tx(
                    guild_id=interaction.guild_id,
                    kind="union_donate",
                    account_type="union",
                    account_id=interaction.guild_id,
                    account_name="Workers' Union",
                    counterparty_type=atype,
                    counterparty_id=row["id"],
                    counterparty_name=row["name"],
                    amount=amount,
                    balance_after=union_after,
                    actor_id=interaction.user.id,
                    note=f"Donation from {row['name']}",
                )
                await self.db.record_faction_donation(
                    guild_id=interaction.guild_id,
                    account_type=atype,
                    account_id=row["id"],
                    faction=factions.UNION,
                    kind="credits",
                    amount=amount,
                    cycle=await self.db.current_cycle(interaction.guild_id),
                    actor_id=interaction.user.id,
                    note="Donation to the Workers' Union",
                )
        except InsufficientFunds as exc:
            return await interaction.response.send_message(
                embed=embeds.error(
                    "Insufficient Funds",
                    f"{account_emoji(atype)} **{row['name']}** only holds **{money(exc.balance)}**.",
                ),
                ephemeral=True,
            )

        await interaction.response.send_message(
            embed=embeds.success(
                "Donation Accepted",
                f"{account_emoji(atype)} **{row['name']}** donated **{money(amount)}** "
                f"to the Workers' Union.\nUnion treasury: **{money(union_after)}**",
            )
        )

    # -- membership --------------------------------------------------------
    @group.command(name="join", description="Join the Workers' Union for cheaper building blocks and food.")
    @app_commands.describe(account="The nation or kingdom joining.")
    @app_commands.autocomplete(account=account_autocomplete)
    async def join(self, interaction: discord.Interaction, account: str):
        atype, row = await resolve_account(self.db, interaction.guild_id, account)
        if not row:
            return await interaction.response.send_message(
                embed=embeds.error("Not Found", "That account does not exist."), ephemeral=True
            )
        if not await self._can_act(interaction, row):
            return await interaction.response.send_message(
                embed=self._not_authorized(atype, row), ephemeral=True
            )
        try:
            result = await factions.join_faction(
                self.db,
                guild_id=interaction.guild_id,
                account_type=atype,
                account_id=row["id"],
                faction=factions.UNION,
                actor_id=interaction.user.id,
            )
        except factions.FactionError as exc:
            return await interaction.response.send_message(
                embed=embeds.error("Allegiance Refused", str(exc)), ephemeral=True
            )
        if result.already:
            return await interaction.response.send_message(
                embed=embeds.info(
                    "Already a Member",
                    f"{account_emoji(atype)} **{row['name']}** already belongs to the Union.",
                ),
                ephemeral=True,
            )

        await self.db.log_tx(
            guild_id=interaction.guild_id,
            kind="union_join",
            account_type=atype,
            account_id=row["id"],
            account_name=row["name"],
            actor_id=interaction.user.id,
            note="Joined the Workers' Union",
        )
        extra = ""
        if result.previous == factions.CROWN:
            extra = "\n\nLeft **The Crown**."
            if result.cancelled_contracts:
                extra += (
                    f" **{result.cancelled_contracts}** active Crown contract(s) "
                    "were cancelled."
                )
        await interaction.response.send_message(
            embed=embeds.success(
                "Welcome to the Union",
                f"{account_emoji(atype)} **{row['name']}** is now a Union member.{extra}\n\n"
                f"**{config.UNION_MEMBER_DISCOUNT}% off** "
                f"{', '.join(sorted(config.UNION_MEMBER_DISCOUNT_CATEGORIES))} in the Royal Shop "
                f"(redstone and materials are excluded).\n"
                f"Dues of **{config.union_tax_label(atype)}%** of the whole balance, "
                f"on every payout, go to the Union treasury.",
            ),
        )

    @group.command(name="leave", description="Leave the Workers' Union (discount and dues both stop).")
    @app_commands.describe(account="The nation or kingdom leaving.")
    @app_commands.autocomplete(account=account_autocomplete)
    async def leave(self, interaction: discord.Interaction, account: str):
        atype, row = await resolve_account(self.db, interaction.guild_id, account)
        if not row:
            return await interaction.response.send_message(
                embed=embeds.error("Not Found", "That account does not exist."), ephemeral=True
            )
        if not await self._can_act(interaction, row):
            return await interaction.response.send_message(
                embed=self._not_authorized(atype, row), ephemeral=True
            )
        if not await self.db.remove_union_member(interaction.guild_id, atype, row["id"]):
            return await interaction.response.send_message(
                embed=embeds.info(
                    "Not a Member",
                    f"{account_emoji(atype)} **{row['name']}** does not belong to the Union.",
                ),
                ephemeral=True,
            )

        await self.db.log_tx(
            guild_id=interaction.guild_id,
            kind="union_leave",
            account_type=atype,
            account_id=row["id"],
            account_name=row["name"],
            actor_id=interaction.user.id,
            note="Left the Workers' Union",
        )
        await interaction.response.send_message(
            embed=embeds.success(
                "Left the Union",
                f"{account_emoji(atype)} **{row['name']}** left the Workers' Union. "
                f"The member discount and dues both stop.",
            )
        )

    @group.command(name="members", description="List the accounts that belong to the Union.")
    async def members(self, interaction: discord.Interaction):
        if interaction.guild_id is None:
            return await interaction.response.send_message(
                embed=embeds.error("Guild Only", "The Union only runs inside a server."), ephemeral=True
            )
        rows = await self.db.list_union_members(interaction.guild_id)
        if not rows:
            return await interaction.response.send_message(
                embed=embeds.info("No Members", "No nation or kingdom has joined the Union yet."),
                ephemeral=True,
            )
        lines = []
        for r in rows:
            if r["account_type"] == "nation":
                account = await self.db.get_nation_by_id(r["account_id"])
            else:
                account = await self.db.get_kingdom_by_id(r["account_id"])
            if account is None:
                continue
            lines.append(
                f"{account_emoji(r['account_type'])} **{account['name']}** · "
                f"{account_type_label(r['account_type'])} · joined {r['joined_at'][:10]}"
            )
        if not lines:
            return await interaction.response.send_message(
                embed=embeds.info("No Members", "No nation or kingdom has joined the Union yet."),
                ephemeral=True,
            )
        embed = embeds.info(f"Union Members ({len(lines)})", "\n".join(lines)[:4096])
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Union(bot))
