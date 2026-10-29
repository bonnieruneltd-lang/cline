"""
cogs/fishing.py
===============
The Fishing Guild: ``/fish`` and the house account that pays for each catch.

A catch is a random fish worth 20–100 credits. It is **not** minted: the catch
debits ``fishing_guild_account`` and credits the player's individual wallet in
the same transaction, with a ledger row on both sides. The only ways credits
enter the guild are the admin ``/fishingguild fund`` command; the only way they
leave other than a catch is ``/fishingguild withdraw``.

The player must already have an individual wallet (``/daily`` creates one), and
the 100M individual cap is checked before the guild is debited. If the guild is
short, the catch is refused rather than paid from nowhere.
"""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

import config
import embeds
import fishing
from checks import admin_only
from database import CapExceeded, InsufficientFunds
from utils import money


class FishingRefused(Exception):
    """A user-facing catch refusal discovered under the transaction lock."""


class Fishing(commands.Cog):
    """🎣 The Fishing Guild: a public catch funded by a house balance."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @property
    def db(self):
        return self.bot.db  # type: ignore[attr-defined]

    group = app_commands.Group(
        name="fishingguild", description="🎣 The Fishing Guild house account."
    )

    # ------------------------------------------------------------------
    # /fish
    # ------------------------------------------------------------------
    @app_commands.command(
        name="fish",
        description="Cast a line for a random 20–100 credit fish paid by the Fishing Guild.",
    )
    async def fish(self, interaction: discord.Interaction):
        if interaction.guild_id is None:
            return await interaction.response.send_message(
                embed=embeds.error("Guild Only", "The Fishing Guild only runs inside a server."),
                ephemeral=True,
            )

        wallet = await self.db.find_individual(interaction.guild_id, interaction.user.id)
        if wallet is None:
            return await interaction.response.send_message(
                embed=embeds.info(
                    "No Wallet",
                    "You need an individual balance before you can fish. Claim your first "
                    "credits with `/daily`.",
                ),
                ephemeral=True,
            )

        fish_name, payout = fishing.catch()
        balance = int(wallet["balance"])
        house = await self.db.get_fishing_balance(interaction.guild_id)
        problem = fishing.catch_problem(payout=payout, balance=balance, house=house)
        if problem:
            return await interaction.response.send_message(
                embed=embeds.error("Catch Refused", problem), ephemeral=True
            )

        try:
            async with self.db.transaction():
                # Re-read under the transaction lock: the wallet and the guild
                # balance may have changed while the catch was being prepared.
                # A deleted wallet is never recreated by a catch.
                fresh = await self.db.find_individual(
                    interaction.guild_id, interaction.user.id
                )
                if fresh is None:
                    raise FishingRefused(
                        "Your individual wallet no longer exists. Nothing was caught."
                    )
                fresh_house = await self.db.get_fishing_balance(interaction.guild_id)
                problem = fishing.catch_problem(
                    payout=payout,
                    balance=int(fresh["balance"]),
                    house=fresh_house,
                )
                if problem:
                    raise FishingRefused(problem)

                house_after = await self.db.debit_fishing_guild(
                    interaction.guild_id, payout
                )
                _added, player_after = await self.db.credit_individual_strict(
                    interaction.guild_id,
                    interaction.user.id,
                    payout,
                    display_name=fresh["display_name"],
                )
                label = fresh["display_name"] or str(fresh["discord_id"])
                await self.db.log_tx(
                    guild_id=interaction.guild_id,
                    kind="fishing_catch",
                    account_type="individual",
                    account_id=fresh["id"],
                    account_name=label,
                    counterparty_type="fishing_guild",
                    counterparty_name="Fishing Guild",
                    amount=payout,
                    balance_after=player_after,
                    actor_id=interaction.user.id,
                    note=f"Caught {fish_name}",
                )
                await self.db.log_tx(
                    guild_id=interaction.guild_id,
                    kind="fishing_catch",
                    account_type="fishing_guild",
                    account_id=interaction.guild_id,
                    account_name="Fishing Guild",
                    counterparty_type="individual",
                    counterparty_id=fresh["id"],
                    counterparty_name=label,
                    amount=-payout,
                    balance_after=house_after,
                    actor_id=interaction.user.id,
                    note=f"Paid out for {fish_name}",
                )
        except FishingRefused as exc:
            return await interaction.response.send_message(
                embed=embeds.error("Catch Refused", str(exc)), ephemeral=True
            )
        except InsufficientFunds as exc:
            return await interaction.response.send_message(
                embed=embeds.error(
                    "Fishing Guild Short",
                    f"The Fishing Guild only holds **{money(exc.balance)}** and cannot pay "
                    f"this **{money(payout)}** catch. Nothing was caught.",
                ),
                ephemeral=True,
            )
        except CapExceeded:
            return await interaction.response.send_message(
                embed=embeds.error(
                    "Catch Refused",
                    "That catch would take you past the individual cap. Nothing was caught.",
                ),
                ephemeral=True,
            )

        embed = discord.Embed(
            title="🎣 A Catch!",
            description=(
                f"**{interaction.user.display_name}** hauls in a **{fish_name}**.\n"
                f"The Fishing Guild pays **{money(payout)}** across the dock."
            ),
            color=config.GOLD_COLOR,
        )
        embed.add_field(name="Catch", value=fish_name, inline=True)
        embed.add_field(name="Value", value=money(payout), inline=True)
        embed.add_field(name="Your Balance", value=money(player_after), inline=True)
        embed.add_field(name="Fishing Guild", value=money(house_after), inline=True)
        embed.set_footer(text="⚜ The Royal Court · Fishing Guild · every catch is public")
        await interaction.response.send_message(embed=embed)

    # ------------------------------------------------------------------
    # /fishingguild balance · fund · withdraw
    # ------------------------------------------------------------------
    @group.command(
        name="balance",
        description="Show the Fishing Guild balance that pays every catch.",
    )
    async def balance(self, interaction: discord.Interaction):
        if interaction.guild_id is None:
            return await interaction.response.send_message(
                embed=embeds.error("Guild Only", "The Fishing Guild only runs inside a server."),
                ephemeral=True,
            )
        house = await self.db.get_fishing_balance(interaction.guild_id)
        embed = embeds.info(
            "🎣 The Fishing Guild",
            "Every `/fish` catch is paid from this balance. The guild cannot pay a "
            "catch it cannot cover, and only an administrator can add or remove "
            "credits.",
        )
        embed.add_field(name="Guild Balance", value=f"## {money(house)}", inline=False)
        embed.add_field(
            name="Catch Range",
            value=f"**{money(config.FISHING_MIN_CREDITS)}–{money(config.FISHING_MAX_CREDITS)}** per fish",
            inline=True,
        )
        embed.add_field(
            name="Funding",
            value="`/fishingguild fund` [Admin] · `/fishingguild withdraw` [Admin]",
            inline=False,
        )
        await interaction.response.send_message(embed=embed)

    @group.command(
        name="fund",
        description="[Admin] Credit the Fishing Guild so it can pay catches.",
    )
    @app_commands.describe(amount="Credits to add to the Fishing Guild.")
    @admin_only()
    async def fund(
        self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, None]
    ):
        if interaction.guild_id is None:
            return await interaction.response.send_message(
                embed=embeds.error("Guild Only", "The Fishing Guild only runs inside a server."),
                ephemeral=True,
            )
        async with self.db.transaction():
            after = await self.db.credit_fishing_guild(interaction.guild_id, amount)
            await self.db.log_tx(
                guild_id=interaction.guild_id,
                kind="fishing_guild_fund",
                account_type="fishing_guild",
                account_id=interaction.guild_id,
                account_name="Fishing Guild",
                amount=amount,
                balance_after=after,
                actor_id=interaction.user.id,
                note="Admin funded the Fishing Guild",
            )
        await interaction.response.send_message(
            embed=embeds.success(
                "Fishing Guild Funded",
                f"**{money(amount)}** added to the Fishing Guild.\n"
                f"New balance: **{money(after)}**",
            )
        )

    @group.command(
        name="withdraw",
        description="[Admin] Remove credits from the Fishing Guild.",
    )
    @app_commands.describe(amount="Credits to take out of the Fishing Guild.")
    @admin_only()
    async def withdraw(
        self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, None]
    ):
        if interaction.guild_id is None:
            return await interaction.response.send_message(
                embed=embeds.error("Guild Only", "The Fishing Guild only runs inside a server."),
                ephemeral=True,
            )
        try:
            async with self.db.transaction():
                after = await self.db.debit_fishing_guild(interaction.guild_id, amount)
                await self.db.log_tx(
                    guild_id=interaction.guild_id,
                    kind="fishing_guild_withdraw",
                    account_type="fishing_guild",
                    account_id=interaction.guild_id,
                    account_name="Fishing Guild",
                    amount=-amount,
                    balance_after=after,
                    actor_id=interaction.user.id,
                    note="Admin withdrew from the Fishing Guild",
                )
        except InsufficientFunds:
            house = await self.db.get_fishing_balance(interaction.guild_id)
            return await interaction.response.send_message(
                embed=embeds.error(
                    "Insufficient Funds",
                    f"The Fishing Guild holds **{money(house)}**. Nothing was withdrawn.",
                ),
                ephemeral=True,
            )
        await interaction.response.send_message(
            embed=embeds.success(
                "Fishing Guild Withdrawn",
                f"**{money(amount)}** taken out of the Fishing Guild.\n"
                f"New balance: **{money(after)}**",
            )
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Fishing(bot))

