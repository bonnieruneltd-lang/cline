"""
cogs/bank.py
============
The Royal Bank.

Rules implemented here:
  * The bank moves credits between nation treasuries, kingdom treasuries and
    individual wallets. Every direction is handled by ``account_transfers``.
  * Money can be moved freely between accounts inside the system.
  * Turning money into real in-game currency requires an ADMIN:
        - /bank deposit  : a player hands in-game currency to an admin,
                           who credits it into a nation/kingdom's bank.
        - /bank withdraw : an admin hands in-game currency to a player,
                           debiting the nation/kingdom's bank.
  * Anyone may give money to anyone (transfers), BUT you may only move
    money OUT of a treasury you lead, your own individual wallet, or as an
    administrator.
  * A nation may DRAW from its kingdom's balance, but the kingdom leader
    must approve (via a DM with buttons). Adding money to a kingdom is fine.
"""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

import account_transfers as transfers
import embeds
from checks import admin_only
from database import AccountMissing, CapExceeded, InsufficientFunds
from payouts import kingdom_payout, nation_payout
from utils import (
    account_autocomplete,
    account_emoji,
    any_account_autocomplete,
    is_admin,
    kingdom_autocomplete,
    money,
    parse_amount,
    resolve_account,
)


# ---------------------------------------------------------------------------
# Persistent approval view for kingdom draws
# ---------------------------------------------------------------------------
class DrawApprovalView(discord.ui.View):
    """Approve/Deny buttons DM'd to a kingdom leader for a nation's draw."""

    def __init__(self, bot: commands.Bot, draw_id: int) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.draw_id = draw_id

        approve = discord.ui.Button(
            label="Approve", style=discord.ButtonStyle.success, emoji="✅",
            custom_id=f"draw:approve:{draw_id}",
        )
        deny = discord.ui.Button(
            label="Deny", style=discord.ButtonStyle.danger, emoji="⛔",
            custom_id=f"draw:deny:{draw_id}",
        )
        approve.callback = self._approve
        deny.callback = self._deny
        self.add_item(approve)
        self.add_item(deny)

    async def _finalize(self, interaction: discord.Interaction, approve: bool) -> None:
        db = self.bot.db  # type: ignore[attr-defined]
        draw = await db.get_pending_draw(self.draw_id)
        if not draw or draw["status"] != "pending":
            return await interaction.response.send_message(
                embed=embeds.error("Already Handled", "This request has already been resolved."), ephemeral=True
            )

        kingdom = await db.get_kingdom_by_id(draw["kingdom_id"])
        nation = await db.get_nation_by_id(draw["nation_id"])
        if not kingdom or not nation:
            async with db.transaction():
                await db.claim_pending_draw(self.draw_id, "invalid")
            return await interaction.response.send_message(
                embed=embeds.error("Gone", "The kingdom or nation no longer exists."), ephemeral=True
            )

        # Only the kingdom leader (or an admin) may decide.
        is_leader = kingdom["leader_id"] == interaction.user.id
        is_adm = bool(getattr(interaction.user, "guild_permissions", None)
                      and interaction.user.guild_permissions.administrator)
        if not (is_leader or is_adm):
            return await interaction.response.send_message(
                embed=embeds.error("Not Authorized", "Only the kingdom leader may decide this."), ephemeral=True
            )

        if not approve:
            async with db.transaction():
                if not await db.claim_pending_draw(self.draw_id, "denied"):
                    outcome = "handled"
                else:
                    outcome = "denied"
                    await db.log_tx(
                        guild_id=draw["guild_id"], kind="draw_denied",
                        account_type="kingdom", account_id=kingdom["id"], account_name=kingdom["name"],
                        counterparty_type="nation", counterparty_id=nation["id"], counterparty_name=nation["name"],
                        amount=0, actor_id=interaction.user.id,
                        note=f"Request for {draw['amount']} denied; requested by <@{draw['requested_by']}>",
                    )
            if outcome == "handled":
                return await interaction.response.send_message(
                    embed=embeds.error("Already Handled", "This request has already been resolved."),
                    ephemeral=True,
                )
            await interaction.response.edit_message(
                embed=embeds.error(
                    "Draw Denied",
                    f"Your request for **{money(draw['amount'])}** from 👑 **{kingdom['name']}** was denied.",
                ),
                view=None,
            )
            await self._notify(draw["requested_by"], embeds.error(
                "Draw Denied",
                f"Your nation 🏛️ **{nation['name']}**'s request for **{money(draw['amount'])}** "
                f"from 👑 **{kingdom['name']}** was **denied**.",
            ))
            return

        # Flipping the status, moving the credits and writing both ledger rows
        # is one transaction: a double click cannot pay the draw twice.
        outcome = None
        n_committed = 0
        fresh_kingdom = kingdom
        fresh_nation = nation
        amount = int(draw["amount"])
        try:
            async with db.transaction():
                # Re-read the request, both accounts and the membership inside
                # the transaction. The DM may have sat unclicked while the nation
                # left the kingdom or the treasury changed; trusting the rows
                # read above would pay a draw that is no longer valid.
                fresh_draw = await db.get_pending_draw(self.draw_id)
                fresh_kingdom = (
                    await db.get_kingdom_by_id(draw["kingdom_id"]) if fresh_draw else None
                )
                fresh_nation = (
                    await db.get_nation_by_id(draw["nation_id"]) if fresh_draw else None
                )
                if not fresh_draw or fresh_draw["status"] != "pending":
                    outcome = "handled"
                elif fresh_kingdom is None or fresh_nation is None:
                    await db.claim_pending_draw(self.draw_id, "invalid")
                    outcome = "invalid"
                elif (
                    int(fresh_kingdom["guild_id"]) != int(fresh_draw["guild_id"])
                    or int(fresh_nation["guild_id"]) != int(fresh_draw["guild_id"])
                ):
                    await db.claim_pending_draw(self.draw_id, "invalid")
                    outcome = "invalid"
                elif int(fresh_nation["kingdom_id"] or 0) != int(fresh_kingdom["id"]):
                    # The nation was removed from the kingdom after asking.
                    # Nothing may move to an ex-member.
                    await db.claim_pending_draw(self.draw_id, "invalid")
                    outcome = "not_member"
                elif not (fresh_kingdom["leader_id"] == interaction.user.id or is_adm):
                    # Leadership changed; leave it pending for the new leader.
                    outcome = "unauthorized"
                elif int(fresh_kingdom["balance"]) < int(fresh_draw["amount"]):
                    await db.claim_pending_draw(self.draw_id, "failed")
                    outcome = "short"
                elif not await db.claim_pending_draw(self.draw_id, "approved"):
                    outcome = "handled"
                else:
                    outcome = "paid"
                    amount = int(fresh_draw["amount"])
                    k_after, n_after = await db.transfer_balance(
                        "kingdoms", fresh_kingdom["id"], "nations", fresh_nation["id"], amount
                    )
                    await db.log_tx(
                        guild_id=fresh_draw["guild_id"], kind="draw",
                        account_type="kingdom", account_id=fresh_kingdom["id"], account_name=fresh_kingdom["name"],
                        counterparty_type="nation", counterparty_id=fresh_nation["id"], counterparty_name=fresh_nation["name"],
                        amount=-amount, balance_after=k_after, actor_id=interaction.user.id,
                        note=f"Approved draw by <@{fresh_draw['requested_by']}>",
                    )
                    await db.log_tx(
                        guild_id=fresh_draw["guild_id"], kind="draw",
                        account_type="nation", account_id=fresh_nation["id"], account_name=fresh_nation["name"],
                        counterparty_type="kingdom", counterparty_id=fresh_kingdom["id"], counterparty_name=fresh_kingdom["name"],
                        amount=amount, balance_after=n_after, actor_id=interaction.user.id,
                        note="Approved by current kingdom leader",
                    )
                    n_committed = await db.commit_balance_to_union(
                        fresh_draw["guild_id"], "nation", fresh_nation["id"]
                    )
        except AccountMissing:
            async with db.transaction():
                await db.claim_pending_draw(self.draw_id, "failed")
            return await interaction.response.edit_message(
                embed=embeds.error(
                    "Account Gone",
                    "The kingdom or nation was deleted before the draw could be approved.",
                ),
                view=None,
            )
        except InsufficientFunds as exc:
            async with db.transaction():
                await db.claim_pending_draw(self.draw_id, "failed")
            name = fresh_kingdom["name"] if fresh_kingdom else kingdom["name"]
            return await interaction.response.edit_message(
                embed=embeds.error(
                    "Insufficient Funds",
                    f"👑 **{name}** only has **{money(exc.balance)}** — "
                    f"cannot honor **{money(amount)}**.",
                ),
                view=None,
            )

        if outcome == "handled":
            return await interaction.response.send_message(
                embed=embeds.error("Already Handled", "This request has already been resolved."),
                ephemeral=True,
            )
        if outcome == "invalid":
            return await interaction.response.edit_message(
                embed=embeds.error(
                    "Gone",
                    "The kingdom or nation no longer exists. No credits moved.",
                ),
                view=None,
            )
        if outcome == "not_member":
            return await interaction.response.edit_message(
                embed=embeds.error(
                    "No Longer a Member",
                    f"🏛️ **{fresh_nation['name']}** is no longer part of "
                    f"👑 **{fresh_kingdom['name']}**, so this draw is invalid. "
                    "No credits moved.",
                ),
                view=None,
            )
        if outcome == "unauthorized":
            return await interaction.response.send_message(
                embed=embeds.error(
                    "Not Authorized",
                    "Only the current kingdom leader may decide this.",
                ),
                ephemeral=True,
            )
        if outcome == "short":
            return await interaction.response.edit_message(
                embed=embeds.error(
                    "Insufficient Funds",
                    f"👑 **{fresh_kingdom['name']}** only has "
                    f"**{money(fresh_kingdom['balance'])}** — "
                    f"cannot honor **{money(amount)}**.",
                ),
                view=None,
            )

        committed_note = (
            f"\nCommitted to the Union: **{money(n_committed)}**"
            if n_committed
            else ""
        )
        await interaction.response.edit_message(
            embed=embeds.success(
                "Draw Approved",
                f"**{money(amount)}** moved from 👑 **{fresh_kingdom['name']}** to "
                f"🏛️ **{fresh_nation['name']}**.{committed_note}",
            ),
            view=None,
        )
        await self._notify(draw["requested_by"], embeds.success(
            "Draw Approved",
            f"Your nation 🏛️ **{fresh_nation['name']}**'s request for **{money(amount)}** "
            f"from 👑 **{fresh_kingdom['name']}** was **approved**.",
        ))

    async def _approve(self, interaction: discord.Interaction) -> None:
        await self._finalize(interaction, approve=True)

    async def _deny(self, interaction: discord.Interaction) -> None:
        await self._finalize(interaction, approve=False)

    async def _notify(self, user_id: int, embed: discord.Embed) -> None:
        try:
            user = await self.bot.fetch_user(user_id)
            await user.send(embed=embed)
        except Exception:
            pass


class Bank(commands.Cog):
    """🏦 The Royal Bank — deposits, withdrawals, transfers & draws."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @property
    def db(self):
        return self.bot.db  # type: ignore[attr-defined]

    group = app_commands.Group(name="bank", description="🏦 The Royal Bank of the realm.")

    # -- helpers -----------------------------------------------------------
    async def _next_payout(self, account_type: str, row) -> int:
        if account_type == "nation":
            return nation_payout(
                row["happy_workers"], row["regular_workers"],
                row["happy_soldiers"], row["regular_soldiers"],
            ).total
        members = await self.db.nations_in_kingdom(row["id"])
        return kingdom_payout(members).total

    # -- balance -----------------------------------------------------------
    @group.command(name="balance", description="View a nation's or kingdom's Royal Bank balance.")
    @app_commands.describe(account="Pick a nation or kingdom.")
    @app_commands.autocomplete(account=account_autocomplete)
    async def balance(self, interaction: discord.Interaction, account: str):
        atype, row = await resolve_account(self.db, interaction.guild_id, account)
        if not row:
            return await interaction.response.send_message(
                embed=embeds.error("Not Found", "That account does not exist."), ephemeral=True
            )
        nxt = await self._next_payout(atype, row)
        embed = embeds.account_embed(atype, row, next_payout=nxt)
        await interaction.response.send_message(embed=embed)

    # -- deposit (admin) ---------------------------------------------------
    @group.command(name="deposit", description="[Admin] Credit in-game currency into an account's Royal Bank.")
    @app_commands.describe(account="The account receiving the deposit.", amount="Amount of credits.")
    @app_commands.autocomplete(account=account_autocomplete)
    @admin_only()
    async def deposit(self, interaction: discord.Interaction, account: str, amount: int):
        atype, row = await resolve_account(self.db, interaction.guild_id, account)
        if not row:
            return await interaction.response.send_message(
                embed=embeds.error("Not Found", "That account does not exist."), ephemeral=True
            )
        if amount <= 0:
            return await interaction.response.send_message(
                embed=embeds.error("Invalid Amount", "Amount must be greater than zero."), ephemeral=True
            )
        try:
            async with self.db.transaction():
                after = await self.db.add_balance(
                    "nations" if atype == "nation" else "kingdoms", row["id"], amount
                )
                await self.db.log_tx(
                    guild_id=interaction.guild_id, kind="deposit",
                    account_type=atype, account_id=row["id"], account_name=row["name"],
                    amount=amount, balance_after=after, actor_id=interaction.user.id,
                    note="Admin credited in-game currency to the Royal Bank",
                )
                committed = await self.db.commit_balance_to_union(
                    interaction.guild_id, atype, row["id"]
                )
        except AccountMissing:
            return await interaction.response.send_message(
                embed=embeds.error("Account Gone", "That account was deleted before the deposit landed."),
                ephemeral=True,
            )
        note = (
            f"\nCommitted to the Union: **{money(committed)}**"
            if committed
            else ""
        )
        await interaction.response.send_message(
            embed=embeds.success(
                "Deposit Recorded",
                f"{account_emoji(atype)} **{row['name']}** received **{money(amount)}**.\n"
                f"New balance: **{money(after - committed)}**{note}",
            )
        )

    # -- withdraw (admin) --------------------------------------------------
    @group.command(name="withdraw", description="[Admin] Hand in-game currency to a nation/kingdom, debiting its bank.")
    @app_commands.describe(account="The account cashing out.",
                           amount="Amount of credits, or all / half / quarter of the balance.")
    @app_commands.autocomplete(account=account_autocomplete)
    @admin_only()
    async def withdraw(self, interaction: discord.Interaction, account: str, amount: str):
        atype, row = await resolve_account(self.db, interaction.guild_id, account)
        if not row:
            return await interaction.response.send_message(
                embed=embeds.error("Not Found", "That account does not exist."), ephemeral=True
            )
        try:
            amount = parse_amount(amount, int(row["balance"]))
        except ValueError as exc:
            return await interaction.response.send_message(
                embed=embeds.error("Invalid Amount", str(exc)), ephemeral=True
            )
        if row["balance"] < amount:
            return await interaction.response.send_message(
                embed=embeds.error(
                    "Insufficient Funds",
                    f"{account_emoji(atype)} **{row['name']}** only holds **{money(row['balance'])}**.",
                ),
                ephemeral=True,
            )
        try:
            async with self.db.transaction():
                after = await self.db.debit(
                    "nations" if atype == "nation" else "kingdoms", row["id"], amount
                )
                await self.db.log_tx(
                    guild_id=interaction.guild_id, kind="withdraw",
                    account_type=atype, account_id=row["id"], account_name=row["name"],
                    amount=-amount, balance_after=after, actor_id=interaction.user.id,
                    note="Admin paid out in-game currency from the Royal Bank",
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
                "Withdrawal Recorded",
                f"{account_emoji(atype)} **{row['name']}** cashed out **{money(amount)}**.\n"
                f"Remaining balance: **{money(after)}**",
            )
        )

    # -- setbalance (admin) ------------------------------------------------
    @group.command(name="setbalance", description="[Admin] Manually set an account's Royal Bank balance.")
    @app_commands.describe(account="The account to edit.", amount="The exact new balance.")
    @app_commands.autocomplete(account=account_autocomplete)
    @admin_only()
    async def setbalance(self, interaction: discord.Interaction, account: str, amount: int):
        atype, row = await resolve_account(self.db, interaction.guild_id, account)
        if not row:
            return await interaction.response.send_message(
                embed=embeds.error("Not Found", "That account does not exist."), ephemeral=True
            )
        if amount < 0:
            return await interaction.response.send_message(
                embed=embeds.error("Invalid Amount", "Balance cannot be negative."), ephemeral=True
            )
        # The balance edit and its ledger row must land together: a crash
        # between them would leave an account that no longer reconciles.
        fresh = None
        old = 0
        async with self.db.transaction():
            # Re-read the balance inside the transaction. ``row`` was read
            # before the lock, so a concurrent deposit would otherwise make the
            # logged delta (and therefore the ledger sum) wrong.
            fresh = (
                await self.db.get_nation_by_id(row["id"])
                if atype == "nation"
                else await self.db.get_kingdom_by_id(row["id"])
            )
            if fresh is not None:
                old = int(fresh["balance"])
                await self.db.set_balance(
                    "nations" if atype == "nation" else "kingdoms", fresh["id"], amount
                )
                await self.db.log_tx(
                    guild_id=interaction.guild_id, kind="setbalance",
                    account_type=atype, account_id=fresh["id"], account_name=fresh["name"],
                    amount=amount - old, balance_after=amount, actor_id=interaction.user.id,
                    note=f"Manual edit: {old} -> {amount}",
                )
        # Reply only after the commit: a slow or failed Discord response must
        # not hold BEGIN IMMEDIATE over the whole bot, and must not roll back
        # an edit the admin believes was applied.
        if fresh is None:
            return await interaction.response.send_message(
                embed=embeds.error("Not Found", "That account no longer exists."),
                ephemeral=True,
            )
        await interaction.response.send_message(
            embed=embeds.success(
                "Balance Edited",
                f"{account_emoji(atype)} **{fresh['name']}** balance set to **{money(amount)}** "
                f"(was {money(old)}).",
            )
        )

    # -- transfer ----------------------------------------------------------
    @group.command(
        name="transfer",
        description="Move credits between individual wallets, nations and kingdoms.",
    )
    @app_commands.describe(
        source="Account the money comes FROM.",
        destination="Account the money goes TO.",
        amount="Amount of credits, or all / half / quarter of the source balance.",
    )
    @app_commands.autocomplete(
        source=any_account_autocomplete, destination=any_account_autocomplete
    )
    async def transfer(self, interaction: discord.Interaction, source: str, destination: str, amount: str):
        if interaction.guild_id is None:
            return await interaction.response.send_message(
                embed=embeds.error("Guild Only", "Transfers only work inside a server."),
                ephemeral=True,
            )
        s_ref = await transfers.resolve_ref(self.db, interaction.guild_id, source)
        d_ref = await transfers.resolve_ref(self.db, interaction.guild_id, destination)
        if not s_ref or not d_ref:
            return await interaction.response.send_message(
                embed=embeds.error(
                    "Not Found",
                    "One of those accounts does not exist. A member needs an individual "
                    "wallet (created by `/daily`) before sending or receiving a transfer.",
                ),
                ephemeral=True,
            )
        if transfers.same_account(s_ref, d_ref):
            return await interaction.response.send_message(
                embed=embeds.error("Invalid", "Source and destination are the same account."),
                ephemeral=True,
            )
        try:
            amount_int = parse_amount(amount, s_ref.balance)
        except ValueError as exc:
            return await interaction.response.send_message(
                embed=embeds.error("Invalid Amount", str(exc)), ephemeral=True
            )
        admin = is_admin(interaction)
        if not transfers.may_spend(interaction.user.id, admin, s_ref):
            return await interaction.response.send_message(
                embed=embeds.error(
                    "Not Authorized",
                    "Only the leader of that treasury, the owner of that wallet, "
                    "or an admin may move credits out of it.",
                ),
                ephemeral=True,
            )
        try:
            result = await transfers.transfer_funds(
                self.db,
                guild_id=interaction.guild_id,
                actor_id=interaction.user.id,
                is_admin=admin,
                source=s_ref,
                destination=d_ref,
                amount=amount_int,
                kind="transfer",
                note="Royal Bank transfer",
            )
        except transfers.TransferError as exc:
            return await interaction.response.send_message(
                embed=embeds.error("Transfer Refused", str(exc)), ephemeral=True
            )
        except AccountMissing:
            return await interaction.response.send_message(
                embed=embeds.error(
                    "Account Gone",
                    "One of those accounts was deleted before the transfer landed; "
                    "nothing moved.",
                ),
                ephemeral=True,
            )
        except CapExceeded as exc:
            return await interaction.response.send_message(
                embed=embeds.error(
                    "Recipient At Cap",
                    f"The destination can only hold **{money(exc.room)}** more; "
                    "nothing was transferred.",
                ),
                ephemeral=True,
            )
        except InsufficientFunds as exc:
            return await interaction.response.send_message(
                embed=embeds.error(
                    "Insufficient Funds",
                    f"The source only holds **{money(exc.balance)}**.",
                ),
                ephemeral=True,
            )
        committed_note = (
            f"\nCommitted to the Union: **{money(result.committed)}**"
            if result.committed
            else ""
        )
        await interaction.response.send_message(
            embed=embeds.success(
                "Transfer Complete",
                f"{transfers.ref_label(result.source)} → "
                f"{transfers.ref_label(result.destination)}\n"
                f"Amount: **{money(result.amount)}**{committed_note}\n"
                f"Source balance: **{money(result.source_balance)}**\n"
                f"Destination balance: **{money(result.destination_balance)}**",
            )
        )

    # -- draw (nation -> kingdom, needs leader approval) -------------------
    @group.command(name="draw", description="Draw money from your kingdom's treasury (kingdom leader approves).")
    @app_commands.describe(kingdom="The kingdom to draw from.",
                           amount="Amount of credits, or all / half / quarter of the kingdom treasury.")
    @app_commands.autocomplete(kingdom=kingdom_autocomplete)
    async def draw(self, interaction: discord.Interaction, kingdom: str, amount: str):
        krow = await self.db.get_kingdom(interaction.guild_id, kingdom)
        if not krow:
            return await interaction.response.send_message(
                embed=embeds.error("Not Found", f"No kingdom named **{kingdom}**."), ephemeral=True
            )
        try:
            amount = parse_amount(amount, int(krow["balance"]))
        except ValueError as exc:
            return await interaction.response.send_message(
                embed=embeds.error("Invalid Amount", str(exc)), ephemeral=True
            )

        # Find the nation led by the caller.
        nations = await self.db.list_nations(interaction.guild_id)
        nation = next((n for n in nations if n["leader_id"] == interaction.user.id), None)
        if not nation:
            return await interaction.response.send_message(
                embed=embeds.error("No Nation", "You do not lead any nation, so you cannot draw."), ephemeral=True
            )
        if nation["kingdom_id"] != krow["id"]:
            return await interaction.response.send_message(
                embed=embeds.error(
                    "Not a Member",
                    f"🏛️ **{nation['name']}** is not part of 👑 **{krow['name']}**.",
                ),
                ephemeral=True,
            )
        if krow["balance"] < amount:
            return await interaction.response.send_message(
                embed=embeds.error(
                    "Insufficient Funds",
                    f"👑 **{krow['name']}** only holds **{money(krow['balance'])}**.",
                ),
                ephemeral=True,
            )
        if not krow["leader_id"]:
            return await interaction.response.send_message(
                embed=embeds.error("No Leader", f"👑 **{krow['name']}** has no leader to approve this."),
                ephemeral=True,
            )

        draw_id = await self.db.create_pending_draw(
            interaction.guild_id, krow["id"], nation["id"], amount, interaction.user.id
        )
        view = DrawApprovalView(self.bot, draw_id)
        self.bot.add_view(view)  # register persistent view

        embed = embeds.info(
            "Draw Request",
            f"🏛️ **{nation['name']}** requests **{money(amount)}** from 👑 **{krow['name']}**.\n"
            f"Requested by <@{interaction.user.id}>.\n\n"
            f"Kingdom balance: **{money(krow['balance'])}**",
        )
        await interaction.response.send_message(
            embed=embeds.info("Request Sending", "Sending the draw request to the kingdom leader…"),
            ephemeral=True,
        )
        try:
            leader = await self.bot.fetch_user(krow["leader_id"])
            await leader.send(embed=embed, view=view)
        except discord.HTTPException:
            # Forbidden (DMs closed), NotFound (user gone) and transient API
            # errors all leave the same dead row; none may stay "pending".
            async with self.db.transaction():
                await self.db.claim_pending_draw(draw_id, "failed")
            return await interaction.edit_original_response(
                embed=embeds.error(
                    "Cannot Reach Leader",
                    "The kingdom leader could not be messaged, so the request was not delivered.",
                )
            )
        await interaction.edit_original_response(
            embed=embeds.success(
                "Request Sent",
                f"Your draw request for **{money(amount)}** was sent to the leader of 👑 **{krow['name']}**.",
            )
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Bank(bot))
