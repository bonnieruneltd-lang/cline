"""
cogs/individual.py
==================
Personal wallets and the ``/daily`` allowance.

An individual is one member's wallet inside a guild: separate from nation and
kingdom treasuries, open to everyone, hard-capped at
``config.INDIVIDUAL_MAX_CREDITS`` and never eligible for a shop discount.
``/daily`` is the only regular inflow. ``/individual delete`` erases the wallet
and its ledger rows on request.
"""

from __future__ import annotations

import datetime as dt
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import config
import account_transfers as transfers
import allin
import casino
import coinflip
import daily
import embeds
import gambling_ui
from checks import admin_only
from database import AccountMissing, CapExceeded, InsufficientFunds, parse_iso
from utils import (
    account_autocomplete,
    account_emoji,
    is_admin,
    money,
    parse_amount,
    resolve_account,
)


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _stamp(when: dt.datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%S")


class IndividualDeleteConfirm(discord.ui.View):
    """Two-step confirmation before erasing a wallet for good."""

    def __init__(self, cog: "Individuals", author_id: int, target: discord.abc.User) -> None:
        super().__init__(timeout=60)
        self.cog = cog
        self.author_id = author_id
        self.target = target

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                embed=embeds.error("Not Authorized", "This confirmation belongs to someone else."),
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Erase permanently", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        outcome = await self.cog.db.delete_individual_checked(
            interaction.guild_id, self.target.id
        )
        for child in self.children:
            child.disabled = True
        if outcome == "open":
            await interaction.response.edit_message(
                embed=embeds.error(
                    "Hand In Progress",
                    f"{self.target.mention} has an open blackjack or poker hand. "
                    "Settle it (or wait for the timeout) before erasing the wallet.",
                ),
                view=self,
            )
            return
        if outcome == "missing":
            await interaction.response.edit_message(
                embed=embeds.info("Nothing Stored", "There was no individual balance to erase."),
                view=self,
            )
            return
        await interaction.response.edit_message(
            embed=embeds.success(
                "Individual Data Erased",
                f"Every stored credit and ledger row for {self.target.mention} in this server "
                f"has been deleted.",
            ),
            view=self,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            embed=embeds.info("Cancelled", "Nothing was deleted."), view=self
        )


class AllinConfirm(discord.ui.View):
    """Two-step confirmation: an all-in bet empties the wallet on a miss."""

    def __init__(self, cog: "Individuals", author_id: int, number: int, stake: int) -> None:
        super().__init__(timeout=60)
        self.cog = cog
        self.author_id = author_id
        self.number = number
        self.stake = stake
        # A double click can deliver two component interactions before either
        # response lands. ``settle_allin`` flips this flag synchronously; the
        # second click is refused instead of staking a second bet.
        self.used = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                embed=embeds.error("Not Authorized", "This bet belongs to someone else."),
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Bet it all", style=discord.ButtonStyle.danger, emoji="🎰")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        for child in self.children:
            child.disabled = True
        await self.cog.settle_allin(interaction, self.number, self.stake, view=self)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.used:
            return await interaction.response.send_message(
                embed=embeds.info(
                    "Bet Already Settled",
                    "This all-in has already been resolved. Run `/allin` again for a new bet.",
                ),
                ephemeral=True,
            )
        self.used = True
        self.stop()
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            embed=embeds.info("Bet Cancelled", "Nothing was staked."), view=self
        )


class Individuals(commands.Cog):
    """👤 Personal wallets, the daily allowance and data deletion."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @property
    def db(self):
        return self.bot.db  # type: ignore[attr-defined]

    group = app_commands.Group(name="individual", description="👤 Personal balances for every member.")

    # ------------------------------------------------------------------
    # Viewing
    # ------------------------------------------------------------------
    @group.command(name="balance", description="View your individual balance (or another member's).")
    @app_commands.describe(member="Whose balance to show. Defaults to you.")
    async def balance(self, interaction: discord.Interaction, member: Optional[discord.Member] = None):
        if interaction.guild_id is None:
            return await interaction.response.send_message(
                embed=embeds.error("Guild Only", "Individual balances live inside a server."),
                ephemeral=True,
            )
        target = member or interaction.user
        row = await self.db.find_individual(interaction.guild_id, target.id)
        label = f"👤 {target.display_name}"
        if row is None:
            return await interaction.response.send_message(
                embed=embeds.info(
                    f"Individual · {target.display_name}",
                    "No wallet yet. Claim your first credits with `/daily`.",
                ),
                ephemeral=True,
            )

        embed = discord.Embed(title=label, color=config.BRAND_COLOR)
        embed.add_field(name="Balance", value=f"## {money(int(row['balance']))}", inline=False)
        embed.add_field(
            name="Cap",
            value=f"{money(int(row['balance']))} / {money(config.INDIVIDUAL_MAX_CREDITS)}",
            inline=True,
        )
        embed.add_field(
            name="Daily Multiplier",
            value=daily.multiplier_label(int(row["daily_bp"])),
            inline=True,
        )
        embed.add_field(name="Daily Streak", value=f"{int(row['daily_streak'])} day(s)", inline=True)
        embed.add_field(
            name="Last Daily",
            value=f"<t:{int(parse_iso(row['last_daily_at']).timestamp())}:R>"
            if parse_iso(row["last_daily_at"])
            else "_never_",
            inline=False,
        )
        embed.set_footer(text="⚜ The Royal Court · Individual ledger")
        await interaction.response.send_message(embed=embed, ephemeral=member is not None and member.id != interaction.user.id)

    # ------------------------------------------------------------------
    # Admin credit (faucet for corrections, like /bank deposit)
    # ------------------------------------------------------------------
    @group.command(name="give", description="[Admin] Credit an individual balance (respects the 100M cap).")
    @app_commands.describe(member="Who receives the credits.", amount="Credits to add.")
    @admin_only()
    async def give(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        amount: app_commands.Range[int, 1, None],
    ):
        if interaction.guild_id is None:
            return await interaction.response.send_message(
                embed=embeds.error("Guild Only", "Individual balances live inside a server."),
                ephemeral=True,
            )
        async with self.db.transaction():
            row = await self.db.get_individual(interaction.guild_id, member.id, member.display_name)
            added, after = await self.db.credit_individual(
                interaction.guild_id, member.id, amount, display_name=member.display_name
            )
            if added:
                await self.db.log_tx(
                    guild_id=interaction.guild_id,
                    kind="individual_grant",
                    account_type="individual",
                    account_id=row["id"],
                    account_name=member.display_name,
                    amount=added,
                    balance_after=after,
                    actor_id=interaction.user.id,
                    note="Admin credited the individual balance",
                )
        if not added:
            return await interaction.response.send_message(
                embed=embeds.info(
                    "Cap Reached",
                    f"{member.mention} already holds **{money(after)}**, the "
                    f"{money(config.INDIVIDUAL_MAX_CREDITS)} individual cap. Nothing was added.",
                ),
                ephemeral=True,
            )
        capped = added < amount
        await interaction.response.send_message(
            embed=embeds.success(
                "Individual Credited",
                f"{member.mention} received **{money(added)}**.\n"
                f"New balance: **{money(after)}**"
                + (
                    f"\n⚠️ The cap absorbed **{money(amount - added)}**; it was not stored."
                    if capped
                    else ""
                ),
            ),
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # Moving money out of an individual wallet
    # ------------------------------------------------------------------
    @group.command(
        name="transfer",
        description="Send credits from your individual balance to another member.",
    )
    @app_commands.describe(
        member="Who receives the credits.",
        amount="Amount of credits, or all / half / quarter of your balance.",
    )
    async def transfer(self, interaction: discord.Interaction, member: discord.Member, amount: str):
        if interaction.guild_id is None:
            return await interaction.response.send_message(
                embed=embeds.error("Guild Only", "Individual balances live inside a server."),
                ephemeral=True,
            )
        if member.id == interaction.user.id:
            return await interaction.response.send_message(
                embed=embeds.error("Invalid", "You cannot transfer to yourself."), ephemeral=True
            )
        if member.bot:
            return await interaction.response.send_message(
                embed=embeds.error("Invalid", "Bots cannot hold an individual balance."),
                ephemeral=True,
            )

        sender = await self.db.find_individual(interaction.guild_id, interaction.user.id)
        if sender is None:
            return await interaction.response.send_message(
                embed=embeds.info(
                    "No Wallet",
                    "You have no individual balance yet. Claim your first credits with `/daily`.",
                ),
                ephemeral=True,
            )
        try:
            amount_int = parse_amount(amount, int(sender["balance"]))
        except ValueError as exc:
            return await interaction.response.send_message(
                embed=embeds.error("Invalid Amount", str(exc)), ephemeral=True
            )

        # The destination wallet may not exist yet. A virtual ref with id 0 is
        # resolved and created by the transfer core inside its transaction, so a
        # refused credit (cap/overdraw) cannot leave a stray zero wallet behind.
        source = transfers.AccountRef(
            kind=transfers.INDIVIDUAL,
            id=int(sender["id"]),
            name=sender["display_name"] or interaction.user.display_name,
            balance=int(sender["balance"]),
            discord_id=interaction.user.id,
        )
        destination = transfers.AccountRef(
            kind=transfers.INDIVIDUAL,
            id=0,
            name=member.display_name,
            balance=0,
            discord_id=member.id,
        )

        try:
            result = await transfers.transfer_funds(
                self.db,
                guild_id=interaction.guild_id,
                actor_id=interaction.user.id,
                is_admin=is_admin(interaction),
                source=source,
                destination=destination,
                amount=amount_int,
                kind="individual_transfer",
                note="Individual transfer",
            )
        except transfers.TransferError as exc:
            return await interaction.response.send_message(
                embed=embeds.error("Transfer Refused", str(exc)), ephemeral=True
            )
        except InsufficientFunds as exc:
            return await interaction.response.send_message(
                embed=embeds.error(
                    "Insufficient Funds",
                    f"You only hold **{money(exc.balance)}** in your individual balance.",
                ),
                ephemeral=True,
            )
        except AccountMissing:
            return await interaction.response.send_message(
                embed=embeds.error(
                    "No Wallet",
                    "Your individual wallet was erased before the transfer landed. "
                    "Nothing was transferred.",
                ),
                ephemeral=True,
            )
        except CapExceeded as exc:
            return await interaction.response.send_message(
                embed=embeds.error(
                    "Recipient At Cap",
                    f"{member.mention} can only hold **{money(config.INDIVIDUAL_MAX_CREDITS)}** "
                    f"and has room for **{money(exc.room)}**. Nothing was transferred.",
                ),
                ephemeral=True,
            )

        await interaction.response.send_message(
            embed=embeds.success(
                "Transfer Complete",
                f"👤 **{interaction.user.display_name}** → 👤 **{member.display_name}**\n"
                f"Amount: **{money(result.amount)}**\n"
                f"Your balance: **{money(result.source_balance)}**",
            )
        )

    @group.command(
        name="pay",
        description="Send credits from your individual balance to a nation or kingdom treasury.",
    )
    @app_commands.describe(
        account="The nation or kingdom to pay.",
        amount="Amount of credits, or all / half / quarter of your balance.",
    )
    @app_commands.autocomplete(account=account_autocomplete)
    async def pay(self, interaction: discord.Interaction, account: str, amount: str):
        if interaction.guild_id is None:
            return await interaction.response.send_message(
                embed=embeds.error("Guild Only", "Individual balances live inside a server."),
                ephemeral=True,
            )
        atype, row = await resolve_account(self.db, interaction.guild_id, account)
        if not row:
            return await interaction.response.send_message(
                embed=embeds.error("Not Found", "That account does not exist."), ephemeral=True
            )

        sender = await self.db.find_individual(interaction.guild_id, interaction.user.id)
        if sender is None:
            return await interaction.response.send_message(
                embed=embeds.info(
                    "No Wallet",
                    "You have no individual balance yet. Claim your first credits with `/daily`.",
                ),
                ephemeral=True,
            )
        try:
            amount_int = parse_amount(amount, int(sender["balance"]))
        except ValueError as exc:
            return await interaction.response.send_message(
                embed=embeds.error("Invalid Amount", str(exc)), ephemeral=True
            )

        source = transfers.AccountRef(
            kind=transfers.INDIVIDUAL,
            id=int(sender["id"]),
            name=sender["display_name"] or interaction.user.display_name,
            balance=int(sender["balance"]),
            discord_id=interaction.user.id,
        )
        destination = transfers.AccountRef(
            kind=atype,
            id=int(row["id"]),
            name=row["name"],
            balance=int(row["balance"]),
            leader_id=row["leader_id"],
        )

        try:
            result = await transfers.transfer_funds(
                self.db,
                guild_id=interaction.guild_id,
                actor_id=interaction.user.id,
                is_admin=is_admin(interaction),
                source=source,
                destination=destination,
                amount=amount_int,
                kind="individual_payment",
                note="Individual payment to a treasury",
            )
        except transfers.TransferError as exc:
            return await interaction.response.send_message(
                embed=embeds.error("Payment Refused", str(exc)), ephemeral=True
            )
        except AccountMissing:
            return await interaction.response.send_message(
                embed=embeds.error(
                    "Account Gone",
                    f"{account_emoji(atype)} **{row['name']}** was deleted before the payment landed; nothing moved.",
                ),
                ephemeral=True,
            )
        except InsufficientFunds as exc:
            return await interaction.response.send_message(
                embed=embeds.error(
                    "Insufficient Funds",
                    f"You only hold **{money(exc.balance)}** in your individual balance.",
                ),
                ephemeral=True,
            )

        note = (
            f"\nCommitted to the Union: **{money(result.committed)}**"
            if result.committed
            else ""
        )
        await interaction.response.send_message(
            embed=embeds.success(
                "Payment Complete",
                f"👤 **{interaction.user.display_name}** → {account_emoji(atype)} **{row['name']}**\n"
                f"Amount: **{money(result.amount)}**{note}\n"
                f"Your balance: **{money(result.source_balance)}**",
            )
        )

    # ------------------------------------------------------------------
    # Gambling
    # ------------------------------------------------------------------
    @group.command(
        name="coinflip",
        description="Stake credits on a coin flip — 50% to win, even money.",
    )
    @app_commands.describe(
        side="The face you are calling.",
        amount="Credits to stake, or all / half / quarter of your balance.",
    )
    @app_commands.choices(
        side=[
            app_commands.Choice(name="Heads", value=coinflip.HEADS),
            app_commands.Choice(name="Tails", value=coinflip.TAILS),
        ]
    )
    async def coinflip(
        self,
        interaction: discord.Interaction,
        side: app_commands.Choice[str],
        amount: str,
    ):
        if interaction.guild_id is None:
            return await interaction.response.send_message(
                embed=embeds.error("Guild Only", "Individual balances live inside a server."),
                ephemeral=True,
            )

        wallet = await self.db.find_individual(interaction.guild_id, interaction.user.id)
        if wallet is None:
            return await interaction.response.send_message(
                embed=embeds.info(
                    "No Wallet",
                    "You have no individual balance yet. Claim your first credits with `/daily`.",
                ),
                ephemeral=True,
            )

        balance = int(wallet["balance"])
        try:
            stake = parse_amount(amount, balance)
        except ValueError as exc:
            return await interaction.response.send_message(
                embed=embeds.error("Invalid Amount", str(exc)), ephemeral=True
            )

        problem = coinflip.stake_problem(stake, balance)
        if problem:
            return await interaction.response.send_message(
                embed=embeds.error("Bet Refused", problem), ephemeral=True
            )

        gross = coinflip.gross_payout(stake)
        house = await self.db.get_casino_balance(interaction.guild_id)
        cover = casino.wager_problem(
            stake=stake,
            balance=balance,
            gross=gross,
            house=house,
            minimum=config.COINFLIP_MIN_STAKE,
        )
        if cover:
            return await interaction.response.send_message(
                embed=embeds.error("Bet Refused", cover), ephemeral=True
            )

        won, landed = coinflip.resolve(side.value, coinflip.roll())
        outcome = "win" if won else "loss"
        try:
            # Both sides of the wager move in one transaction: the stake into the
            # house and the payout out of it, so a failure never leaves the money
            # half-moved or the Casino paying for a hand it did not book.
            async with self.db.transaction():
                await casino.begin_wager(
                    self.db,
                    guild_id=interaction.guild_id,
                    wallet=wallet,
                    stake=stake,
                    kind="individual_coinflip",
                    actor_id=interaction.user.id,
                    note=f"Coinflip stake: called {side.value}",
                )
                after, house_after = await casino.finish_wager(
                    self.db,
                    guild_id=interaction.guild_id,
                    wallet=wallet,
                    stake=stake,
                    gross=gross,
                    outcome=outcome,
                    kind="individual_coinflip",
                    actor_id=interaction.user.id,
                    note=f"Coinflip: called {side.value}, landed {landed}",
                )
        except InsufficientFunds:
            return await interaction.response.send_message(
                embed=embeds.error(
                    "Insufficient Funds",
                    f"You only hold **{money(balance)}** in your individual balance.",
                ),
                ephemeral=True,
            )
        except AccountMissing:
            return await interaction.response.send_message(
                embed=embeds.error(
                    "No Wallet",
                    "Your individual wallet was erased before the bet landed. Nothing was staked.",
                ),
                ephemeral=True,
            )
        except CapExceeded:
            # wager_problem() already screens this, so reaching here means the
            # balance moved between the read and the bet.
            return await interaction.response.send_message(
                embed=embeds.error(
                    "Bet Refused",
                    "A win would take you past the individual cap. Nothing was staked.",
                ),
                ephemeral=True,
            )

        result_embed = gambling_ui.coinflip_result(
            user_mention=interaction.user.mention,
            called=side.value,
            landed=landed,
            stake=stake,
            gross=gross,
            balance=after,
            house=house_after,
            won=won,
        )
        await interaction.response.send_message(embed=result_embed)
        try:
            message = await interaction.original_response()
        except Exception:
            message = None
        await gambling_ui.add_spectator_reactions(message, good=won)

    # ------------------------------------------------------------------
    # All-in
    # ------------------------------------------------------------------
    async def settle_allin(
        self,
        interaction: discord.Interaction,
        number: int,
        stake: int,
        *,
        view: Optional[discord.ui.View] = None,
    ) -> None:
        """Resolve and settle a confirmed all-in bet, then edit the message."""

        async def reply(embed: discord.Embed, *, good: Optional[bool] = None) -> None:
            if view is not None:
                await interaction.response.edit_message(embed=embed, view=view)
                message = getattr(interaction, "message", None)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
                try:
                    message = await interaction.original_response()
                except Exception:
                    message = None
            if good is not None:
                await gambling_ui.add_spectator_reactions(message, good=good)

        # A confirmation view is one bet. Two clicks can arrive before the first
        # response is sent, so refuse the duplicate here, before any await and
        # before the wallet is read. A view-less call (the tests and any future
        # programmatic settle) keeps its current semantics.
        if view is not None:
            if getattr(view, "used", False):
                return await interaction.response.send_message(
                    embed=embeds.info(
                        "Bet Already Settled",
                        "This all-in has already been resolved. Run `/allin` again "
                        "for a new bet.",
                    ),
                    ephemeral=True,
                )
            view.used = True

        if interaction.guild_id is None:
            return await reply(
                embeds.error("Guild Only", "Individual balances live inside a server.")
            )

        wallet = await self.db.find_individual(interaction.guild_id, interaction.user.id)
        if wallet is None:
            return await reply(
                embeds.info(
                    "No Wallet",
                    "You have no individual balance yet. Claim your first credits with `/daily`.",
                )
            )

        # The balance may have moved while the confirmation sat open, so the
        # stake agreed on screen is honoured only if it is still covered.
        balance = int(wallet["balance"])
        if balance < stake:
            return await reply(
                embeds.error(
                    "Bet Refused",
                    f"Your balance is now **{money(balance)}**, below the "
                    f"**{money(stake)}** you confirmed. Nothing was staked.",
                )
            )

        problem = allin.stake_problem(number, stake, balance)
        if problem:
            return await reply(embeds.error("Bet Refused", problem))

        gross = allin.gross_payout(number, stake)
        house = await self.db.get_casino_balance(interaction.guild_id)
        cover = casino.wager_problem(
            stake=stake,
            balance=balance,
            gross=gross,
            house=house,
            minimum=config.ALLIN_MIN_BALANCE,
        )
        if cover:
            return await reply(embeds.error("Bet Refused", cover))

        won = allin.resolve(number, allin.roll(number))
        outcome = "win" if won else "loss"
        try:
            # The stake into the house and the payout out of it share one
            # transaction, so a bet cannot take the stake without settling.
            async with self.db.transaction():
                await casino.begin_wager(
                    self.db,
                    guild_id=interaction.guild_id,
                    wallet=wallet,
                    stake=stake,
                    kind="individual_allin",
                    actor_id=interaction.user.id,
                    note=f"All-in stake: {number}x at {allin.chance_label(number)}",
                )
                after, house_after = await casino.finish_wager(
                    self.db,
                    guild_id=interaction.guild_id,
                    wallet=wallet,
                    stake=stake,
                    gross=gross,
                    outcome=outcome,
                    kind="individual_allin",
                    actor_id=interaction.user.id,
                    note=f"All-in: {number}x at {allin.chance_label(number)}",
                )
        except InsufficientFunds:
            return await reply(
                embeds.error(
                    "Insufficient Funds",
                    f"You only hold **{money(balance)}** in your individual balance.",
                )
            )
        except AccountMissing:
            return await reply(
                embeds.error(
                    "No Wallet",
                    "Your individual wallet was erased before the bet landed. Nothing was staked.",
                )
            )
        except CapExceeded:
            return await reply(
                embeds.error(
                    "Bet Refused",
                    "A win would take you past the individual cap. Nothing was staked.",
                )
            )

        result_embed = gambling_ui.allin_result(
            user_mention=interaction.user.mention,
            number=number,
            stake=stake,
            gross=gross,
            balance=after,
            house=house_after,
            won=won,
        )
        await reply(result_embed, good=won)

    @app_commands.command(
        name="allin",
        description="Stake your whole individual balance on one draw for an n-times return.",
    )
    @app_commands.describe(
        number="2-100. Pays this many times your stake; the chance is 1 in (1.1 x this).",
    )
    async def allin(
        self,
        interaction: discord.Interaction,
        number: app_commands.Range[int, config.ALLIN_MIN_NUMBER, config.ALLIN_MAX_NUMBER],
    ):
        if interaction.guild_id is None:
            return await interaction.response.send_message(
                embed=embeds.error("Guild Only", "Individual balances live inside a server."),
                ephemeral=True,
            )

        wallet = await self.db.find_individual(interaction.guild_id, interaction.user.id)
        if wallet is None:
            return await interaction.response.send_message(
                embed=embeds.info(
                    "No Wallet",
                    "You have no individual balance yet. Claim your first credits with `/daily`.",
                ),
                ephemeral=True,
            )

        stake = int(wallet["balance"])
        problem = allin.stake_problem(number, stake, stake)
        if problem:
            return await interaction.response.send_message(
                embed=embeds.error("Bet Refused", problem), ephemeral=True
            )

        gross = allin.gross_payout(number, stake)
        house = await self.db.get_casino_balance(interaction.guild_id)
        cover = casino.cover_problem(house, gross)
        if cover:
            return await interaction.response.send_message(
                embed=embeds.error("Bet Refused", cover), ephemeral=True
            )

        chance = allin.chance_label(number)
        embed = gambling_ui.allin_confirm(
            user_mention=interaction.user.mention,
            number=number,
            stake=stake,
            gross=gross,
            chance=chance,
            house=house,
        )
        # Public so the channel can watch the draw; only the button is restricted.
        await interaction.response.send_message(
            embed=embed, view=AllinConfirm(self, interaction.user.id, number, stake)
        )

    # ------------------------------------------------------------------
    # Erase
    # ------------------------------------------------------------------
    @group.command(name="delete", description="Erase an individual balance and its stored history.")
    @app_commands.describe(member="Whose data to erase. Defaults to you.")
    async def delete(self, interaction: discord.Interaction, member: Optional[discord.Member] = None):
        if interaction.guild_id is None:
            return await interaction.response.send_message(
                embed=embeds.error("Guild Only", "Individual balances live inside a server."),
                ephemeral=True,
            )
        target = member or interaction.user
        if target.id != interaction.user.id and not is_admin(interaction):
            return await interaction.response.send_message(
                embed=embeds.error(
                    "Not Authorized",
                    "You can only erase your own individual balance (administrators may erase anyone's).",
                ),
                ephemeral=True,
            )
        row = await self.db.find_individual(interaction.guild_id, target.id)
        if row is None:
            return await interaction.response.send_message(
                embed=embeds.info("Nothing Stored", f"{target.mention} has no individual balance."),
                ephemeral=True,
            )
        view = IndividualDeleteConfirm(self, interaction.user.id, target)
        await interaction.response.send_message(
            embed=embeds.error(
                "Erase Individual Data?",
                f"This permanently deletes {target.mention}'s **{money(int(row['balance']))}** "
                f"balance, its daily streak and **every ledger row it owns in this server**. "
                f"There is no undo.",
            ),
            view=view,
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # /daily
    # ------------------------------------------------------------------
    async def claim_daily(
        self,
        guild_id: int,
        user_id: int,
        display_name: str,
        now: dt.datetime,
    ) -> tuple[daily.DailyOutcome, bool, int, int]:
        """Run one daily claim and return ``(outcome, claimed, added, balance)``.

        The state flip, the credit and the ledger row share one transaction, and
        the flip is guarded on the timestamp we read, so a double click or two
        devices claiming together can only pay once.
        """
        claimed = False
        added = 0
        after = 0
        async with self.db.transaction():
            row = await self.db.get_individual(guild_id, user_id, display_name)
            outcome = daily.evaluate(
                parse_iso(row["last_daily_at"]),
                int(row["daily_streak"]),
                int(row["daily_bp"]),
                now,
            )
            if outcome.eligible:
                claimed = await self.db.claim_individual_daily(
                    guild_id,
                    user_id,
                    expected_last=row["last_daily_at"],
                    new_last=_stamp(now),
                    streak=outcome.streak,
                    multiplier_bp=outcome.multiplier_bp,
                )
                if claimed:
                    added, after = await self.db.credit_individual(
                        guild_id, user_id, outcome.payout, display_name=display_name
                    )
                    if added:
                        await self.db.log_tx(
                            guild_id=guild_id,
                            kind="daily",
                            account_type="individual",
                            account_id=row["id"],
                            account_name=display_name,
                            amount=added,
                            balance_after=after,
                            actor_id=user_id,
                            note=(
                                f"Daily claim · {daily.multiplier_label(outcome.multiplier_bp)} "
                                f"multiplier · streak {outcome.streak}"
                            ),
                        )
        return outcome, claimed, added, after

    @app_commands.command(
        name="daily",
        description="Claim your daily credits. Consecutive days raise the multiplier.",
    )
    async def daily(self, interaction: discord.Interaction):
        if interaction.guild_id is None:
            return await interaction.response.send_message(
                embed=embeds.error("Guild Only", "The daily allowance runs inside a server."),
                ephemeral=True,
            )

        now = _utcnow()
        outcome, claimed, added, after = await self.claim_daily(
            interaction.guild_id, interaction.user.id, interaction.user.display_name, now
        )

        if not outcome.eligible:
            return await interaction.response.send_message(
                embed=embeds.info(
                    "Already Claimed",
                    f"You already claimed today. Come back <t:{int((now + dt.timedelta(seconds=outcome.retry_in)).timestamp())}:R>.",
                ),
                ephemeral=True,
            )
        if not claimed:
            return await interaction.response.send_message(
                embed=embeds.info("Already Claimed", "Another claim just went through."),
                ephemeral=True,
            )

        if added:
            head = f"**{money(added)}** added to your individual balance."
        else:
            head = "Your balance is at the individual cap, so nothing was added."
        lines = [
            head,
            f"Balance: **{money(after)}** / {money(config.INDIVIDUAL_MAX_CREDITS)}",
            f"Multiplier: **{daily.multiplier_label(outcome.multiplier_bp)}**",
            f"Streak: **{outcome.streak} day(s)**",
        ]
        if outcome.missed_days:
            lines.append(
                f"⚠️ Missed **{outcome.missed_days} day(s)** — multiplier reduced by "
                f"{config.DAILY_BP_MISS_PENALTY * outcome.missed_days // 100} points."
            )
        if added < outcome.payout:
            lines.append(
                f"⚠️ The {money(config.INDIVIDUAL_MAX_CREDITS)} cap absorbed "
                f"**{money(outcome.payout - added)}** of this payout."
            )
        embed = embeds.success("Daily Claimed", "\n".join(lines))
        embed.add_field(
            name="Next Claim",
            value=f"<t:{int((now + dt.timedelta(hours=config.DAILY_COOLDOWN_HOURS)).timestamp())}:R>",
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Individuals(bot))
