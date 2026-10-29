"""
cogs/transfers.py
=================
The unified ``/transfer`` command and the ``/give`` treasury grant.

``/transfer`` moves credits in every direction between the three economy account
kinds: individual wallets, nation treasuries and kingdom treasuries. ``/give``
is the treasury-to-member shortcut: a nation or kingdom leader pays a member's
individual wallet without spelling out a destination account. Both delegate the
actual movement to :mod:`account_transfers`, which re-reads both accounts,
enforces the source permission and writes both ledger rows in one transaction.

``/bank transfer`` uses the same core and the same autocomplete, so the old
treasury-to-treasury command now also understands individual wallets.
"""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

import account_transfers as transfers
import config
import embeds
from database import AccountMissing, CapExceeded, InsufficientFunds
from utils import (
    account_autocomplete,
    any_account_autocomplete,
    is_admin,
    money,
    parse_amount,
)


class Transfers(commands.Cog):
    """💸 Unified credit transfers between every economy account."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @property
    def db(self):
        return self.bot.db  # type: ignore[attr-defined]

    @app_commands.command(
        name="transfer",
        description="Move credits between individual wallets, nations and kingdoms.",
    )
    @app_commands.describe(
        source="Account the credits come from.",
        destination="Account the credits go to.",
        amount="Amount of credits, or all / half / quarter of the source balance.",
    )
    @app_commands.autocomplete(
        source=any_account_autocomplete,
        destination=any_account_autocomplete,
    )
    async def transfer(
        self,
        interaction: discord.Interaction,
        source: str,
        destination: str,
        amount: str,
    ):
        if interaction.guild_id is None:
            return await interaction.response.send_message(
                embed=embeds.error("Guild Only", "Transfers only work inside a server."),
                ephemeral=True,
            )

        src = await transfers.resolve_ref(self.db, interaction.guild_id, source)
        if src is None:
            return await interaction.response.send_message(
                embed=embeds.error(
                    "Source Not Found",
                    "No nation, kingdom or individual wallet matches that source. "
                    "A member needs a wallet (created by `/daily`) before sending or "
                    "receiving a transfer.",
                ),
                ephemeral=True,
            )
        dst = await transfers.resolve_ref(self.db, interaction.guild_id, destination)
        if dst is None:
            return await interaction.response.send_message(
                embed=embeds.error(
                    "Destination Not Found",
                    "No nation, kingdom or individual wallet matches that destination. "
                    "A member needs a wallet (created by `/daily`) before sending or "
                    "receiving a transfer.",
                ),
                ephemeral=True,
            )
        if transfers.same_account(src, dst):
            return await interaction.response.send_message(
                embed=embeds.error(
                    "Invalid", "Source and destination are the same account."
                ),
                ephemeral=True,
            )

        try:
            amount_int = parse_amount(amount, src.balance)
        except ValueError as exc:
            return await interaction.response.send_message(
                embed=embeds.error("Invalid Amount", str(exc)), ephemeral=True
            )

        admin = is_admin(interaction)
        if not transfers.may_spend(interaction.user.id, admin, src):
            return await interaction.response.send_message(
                embed=embeds.error(
                    "Not Authorized",
                    "You can only move credits out of a treasury you lead or your "
                    "own wallet; administrators may move any account.",
                ),
                ephemeral=True,
            )

        try:
            result = await transfers.transfer_funds(
                self.db,
                guild_id=interaction.guild_id,
                actor_id=interaction.user.id,
                is_admin=admin,
                source=src,
                destination=dst,
                amount=amount_int,
                kind="transfer",
                note="Unified account transfer",
            )
        except transfers.TransferError as exc:
            return await interaction.response.send_message(
                embed=embeds.error("Transfer Refused", str(exc)), ephemeral=True
            )
        except InsufficientFunds as exc:
            return await interaction.response.send_message(
                embed=embeds.error(
                    "Insufficient Funds",
                    f"The source only holds **{money(exc.balance)}**. "
                    "Nothing was transferred.",
                ),
                ephemeral=True,
            )
        except CapExceeded as exc:
            return await interaction.response.send_message(
                embed=embeds.error(
                    "Recipient At Cap",
                    f"The destination has room for **{money(exc.room)}** only. "
                    "Nothing was transferred.",
                ),
                ephemeral=True,
            )
        except AccountMissing:
            return await interaction.response.send_message(
                embed=embeds.error(
                    "Account Gone",
                    "One of the accounts no longer exists. Nothing was transferred.",
                ),
                ephemeral=True,
            )

        lines = [
            f"{transfers.ref_label(result.source)} → "
            f"{transfers.ref_label(result.destination)}",
            f"Amount: **{money(result.amount)}**",
            f"Source balance: **{money(result.source_balance)}**",
            f"Destination balance: **{money(result.destination_balance)}**",
        ]
        if result.committed:
            lines.append(f"Committed to the Union: **{money(result.committed)}**")
        await interaction.response.send_message(
            embed=embeds.success("Transfer Complete", "\n".join(lines))
        )

    @app_commands.command(
        name="give",
        description="Give credits from a nation or kingdom treasury to a member's wallet.",
    )
    @app_commands.describe(
        source="The nation or kingdom paying.",
        member="The member who receives the credits.",
        amount="Amount of credits, or all / half / quarter of the treasury balance.",
    )
    @app_commands.autocomplete(source=account_autocomplete)
    async def give(
        self,
        interaction: discord.Interaction,
        source: str,
        member: discord.Member,
        amount: str,
    ):
        """Move credits from a treasury the caller leads into a member wallet.

        This is the treasury-to-individual shortcut over the same
        ``account_transfers.transfer_funds`` core as ``/transfer``: the
        treasury is debited (never minted), the destination wallet is created
        on first receipt, the Union commitment hook runs, and both ledger rows
        share one transaction.
        """
        if interaction.guild_id is None:
            return await interaction.response.send_message(
                embed=embeds.error(
                    "Guild Only", "Treasury grants only work inside a server."
                ),
                ephemeral=True,
            )
        if member.bot:
            return await interaction.response.send_message(
                embed=embeds.error(
                    "Invalid", "Bots cannot hold an individual balance."
                ),
                ephemeral=True,
            )

        src = await transfers.resolve_ref(self.db, interaction.guild_id, source)
        if src is None or src.kind not in (transfers.NATION, transfers.KINGDOM):
            return await interaction.response.send_message(
                embed=embeds.error(
                    "Source Not Found",
                    "Choose a nation or kingdom treasury to give from.",
                ),
                ephemeral=True,
            )

        admin = is_admin(interaction)
        if not transfers.may_spend(interaction.user.id, admin, src):
            return await interaction.response.send_message(
                embed=embeds.error(
                    "Not Authorized",
                    "Only the treasury's leader or an administrator may give "
                    "credits from it.",
                ),
                ephemeral=True,
            )

        try:
            amount_int = parse_amount(amount, src.balance)
        except ValueError as exc:
            return await interaction.response.send_message(
                embed=embeds.error("Invalid Amount", str(exc)), ephemeral=True
            )

        # A virtual destination ref with id 0 is resolved and created by the
        # transfer core inside its transaction, so a refused credit (cap or
        # overdraw) cannot leave a stray zero wallet behind.
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
                is_admin=admin,
                source=src,
                destination=destination,
                amount=amount_int,
                kind="treasury_give",
                note="Treasury grant to a member",
            )
        except transfers.TransferError as exc:
            return await interaction.response.send_message(
                embed=embeds.error("Grant Refused", str(exc)), ephemeral=True
            )
        except InsufficientFunds as exc:
            return await interaction.response.send_message(
                embed=embeds.error(
                    "Insufficient Funds",
                    f"The treasury only holds **{money(exc.balance)}**. "
                    "Nothing was transferred.",
                ),
                ephemeral=True,
            )
        except CapExceeded as exc:
            return await interaction.response.send_message(
                embed=embeds.error(
                    "Recipient At Cap",
                    f"{member.mention} can only hold "
                    f"**{money(config.INDIVIDUAL_MAX_CREDITS)}** and has room for "
                    f"**{money(exc.room)}**. Nothing was transferred.",
                ),
                ephemeral=True,
            )
        except AccountMissing:
            return await interaction.response.send_message(
                embed=embeds.error(
                    "Account Gone",
                    "The treasury or wallet no longer exists. Nothing was transferred.",
                ),
                ephemeral=True,
            )

        lines = [
            f"{transfers.ref_label(result.source)} → "
            f"{transfers.ref_label(result.destination)}",
            f"Amount: **{money(result.amount)}**",
            f"Treasury balance: **{money(result.source_balance)}**",
            f"Member balance: **{money(result.destination_balance)}**",
        ]
        if result.committed:
            lines.append(f"Committed to the Union: **{money(result.committed)}**")
        await interaction.response.send_message(
            embed=embeds.success("Grant Sent", "\n".join(lines))
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Transfers(bot))

