"""
cogs/factions.py
================
The one faction surface: allegiance, contracts, commitments and the
representatives.

* ``/faction join`` switches a nation/kingdom between the Workers' Union and
  The Crown; ``/faction leave`` leaves the Union.
* ``/faction contract`` is the single contract request. It opens a direct
  negotiation with Gilbert (Workers' Union) or Albert (The Crown); membership
  is never required and no dice are rolled.
* ``/faction commitment`` and ``/faction uncommit`` handle Union standing
  commitments.
* ``/faction donate`` gives credits; ``/faction deliver`` delivers Crown items.
* ``/faction status|contracts|cancel`` reaches Albert, Gilbert and the faction
  ledgers; ``/faction fund|withdraw|members|setchannel`` cover the admin taps.
  Each subcommand exposes only its own options.

No money moves without a ``factions.py`` transaction. The cog only resolves
Discord input, checks who may act for the account, and renders the result.
"""
from __future__ import annotations

from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import config
import embeds
import factions
from checks import admin_only
from database import InsufficientFunds
from shop.autocomplete import item_autocomplete
from utils import (
    account_autocomplete,
    account_emoji,
    is_admin,
    money,
    parse_amount,
    resolve_account,
)


class Factions(commands.Cog):
    """⚜️ Faction allegiance, The Crown's suppliers and Albert."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @property
    def db(self):
        return self.bot.db  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    async def _guild_only(interaction: discord.Interaction) -> bool:
        if interaction.guild_id is not None:
            return False
        await interaction.response.send_message(
            embed=embeds.error("Guild Only", "Factions only operate inside a server."),
            ephemeral=True,
        )
        return True

    async def _account(self, interaction: discord.Interaction, value: str):
        atype, row = await resolve_account(self.db, interaction.guild_id, value)
        if row is None:
            await interaction.response.send_message(
                embed=embeds.error(
                    "Not Found",
                    "No nation or kingdom matches that account.",
                ),
                ephemeral=True,
            )
            return None, None
        return atype, row

    @staticmethod
    def _can_act(interaction: discord.Interaction, row) -> bool:
        return is_admin(interaction) or row["leader_id"] == interaction.user.id

    async def _not_authorized(self, interaction: discord.Interaction, atype: str, row) -> None:
        await interaction.response.send_message(
            embed=embeds.error(
                "Not Authorized",
                f"Only the leader of {account_emoji(atype)} **{row['name']}** "
                "(or an admin) may do that.",
            ),
            ephemeral=True,
        )

    group = app_commands.Group(
        name="crown",
        description="👑 The Crown: Albert's court, supplier contracts and treasury.",
    )

    @staticmethod
    def _clip(text: str, limit: int = 1024) -> str:
        return text if len(text) <= limit else text[: limit - 1] + "…"

    def _status_embed(self, report: dict, atype: str, name: str) -> discord.Embed:
        faction = report.get("faction")
        if faction == factions.CROWN:
            standing = (
                f"**The Crown** · {report['status']} · joined {report['joined_at']}\n"
                f"Soldiers: **{report['soldiers']}** (standing, never a gate)\n"
                f"Crown tax: **{'active' if report['crown_tax_active'] else 'inactive'}** "
                f"— {report['crown_tax_reason']}"
            )
        elif faction:
            standing = (
                f"**Workers' Union** · joined {report['joined_at']}\n"
                f"Soldiers: **{report['soldiers']}** · the Crown keeps no contract."
            )
        else:
            standing = "**Unaffiliated** — use `/faction join` to petition a faction."
        embed = discord.Embed(
            title=f"👑 Royal Orders & Court · {name}",
            description=standing,
            color=config.GOLD_COLOR,
        )
        embed.add_field(
            name="Crown Treasury",
            value=(
                f"**{money(report['crown_balance'])}**\n"
                f"Reserved for active contracts: **{money(report['crown_reserved'])}**\n"
                f"Natural cycle: **{report['cycle']}**"
            ),
            inline=False,
        )
        embed.add_field(
            name="Commitment",
            value=(
                f"{'**Committed**' if report['committed'] else 'Not committed'} · "
                f"**{report['active_contracts']}/{report['contract_slots']}** "
                "active Crown contract(s)"
            ),
            inline=False,
        )
        active = [row for row in report["contracts"] if row["status"] == "active"]
        if active:
            lines = [
                f"#{row['id']} **{row['display_name']}** — "
                f"{row['delivered']}/{row['quantity']} delivered · "
                f"{money(row['value'])} · deadline c{row['deadline_cycle']}"
                for row in active
            ]
            embed.add_field(
                name="Active Contracts", value=self._clip("\n".join(lines)), inline=False
            )
        if report["sales"]:
            lines = [
                f"**{row['item_key']}** — {row['units_sold']} sold · "
                f"demand **{factions.demand_percent(row['demand_bp'])}** · "
                f"supplier until c{row['supplier_until_cycle']}"
                for row in report["sales"][:6]
            ]
            embed.add_field(
                name="Supplier History", value=self._clip("\n".join(lines)), inline=False
            )
        if report["donations"]:
            lines = []
            for row in report["donations"][:5]:
                if row["kind"] == "item":
                    lines.append(
                        f"item **{row['item_key']}** x{row['quantity']} · "
                        f"{money(row['amount'])}"
                    )
                else:
                    lines.append(f"credits **{money(row['amount'])}**")
            embed.add_field(
                name="Recent Donations", value=self._clip("\n".join(lines)), inline=False
            )
        if report["recent_tax"]:
            lines = [
                f"{money(-int(row['amount']))} · {row['ts']}"
                for row in report["recent_tax"][:5]
            ]
            embed.add_field(
                name="Recent Crown Tax", value=self._clip("\n".join(lines)), inline=False
            )
        embed.set_footer(text="Albert · Royal Orders & Court · the record is authoritative")
        return embed

    # ------------------------------------------------------------------
    # /faction · one command group; each action shows only its own options
    # ------------------------------------------------------------------
    faction_group = app_commands.Group(
        name="faction",
        description="Workers' Union and Crown actions.",
    )

    async def _faction_error(
        self, interaction: discord.Interaction, title: str, detail: str
    ) -> None:
        await interaction.response.send_message(
            embed=embeds.error(title, detail), ephemeral=True
        )

    async def _require_admin(
        self, interaction: discord.Interaction, action: str
    ) -> bool:
        """Gate a hub subcommand that delegates to an ``@admin_only`` command.

        ``admin_only`` registers an ``app_commands.check`` on the *command
        object*, and the dispatcher only runs those checks for the command the
        user typed. The ``/faction`` hub subcommands forward to the real
        implementation with ``.callback(...)``, which skips the checks entirely,
        so every delegated admin action has to be gated here as well.
        """
        if is_admin(interaction):
            return True
        await self._faction_error(
            interaction, "Admins Only", f"Only an administrator may {action}."
        )
        return False

    async def _require_faction_member(
        self, interaction: discord.Interaction, account: str, faction: str
    ) -> bool:
        atype, row = await self._account(interaction, account)
        if row is None:
            return False
        member = await self.db.get_faction_member(interaction.guild_id, atype, row["id"])
        if member is None or member["faction"] != faction:
            await self._faction_error(
                interaction,
                "Wrong Faction",
                f"Choose the faction that **{row['name']}** actually serves.",
            )
            return False
        return True

    @faction_group.command(name="join", description="Swear allegiance to the Union or the Crown.")
    @app_commands.describe(faction="Which faction to serve.", account="The nation or kingdom joining.")
    @app_commands.choices(faction=[
        app_commands.Choice(name="⚒️ Workers' Union", value=factions.UNION),
        app_commands.Choice(name="👑 The Crown", value=factions.CROWN),
    ])
    @app_commands.autocomplete(account=account_autocomplete)
    async def faction_join(self, interaction: discord.Interaction, faction: app_commands.Choice[str], account: str):
        await self.join.callback(self, interaction, faction, account)

    @faction_group.command(name="leave", description="Leave the Workers' Union.")
    @app_commands.describe(account="The nation or kingdom leaving.")
    @app_commands.autocomplete(account=account_autocomplete)
    async def faction_leave(self, interaction: discord.Interaction, account: str):
        union = self.bot.get_cog("Union")
        if union is None:
            return await self._faction_error(interaction, "Union Unavailable", "The Union desk is not loaded.")
        await union.leave.callback(union, interaction, account)

    @faction_group.command(
        name="contract",
        description="Request a contract; the faction's representative negotiates it with you.",
    )
    @app_commands.describe(
        faction="Which faction to request from.",
        account="The nation or kingdom requesting the contract.",
        proposal="Write your offer: what you will provide, quantities and timing.",
        budget="Requested budget in credits.",
    )
    @app_commands.choices(faction=[
        app_commands.Choice(name="⚒️ Workers' Union", value=factions.UNION),
        app_commands.Choice(name="👑 The Crown", value=factions.CROWN),
    ])
    @app_commands.autocomplete(account=account_autocomplete)
    async def faction_contract(
        self,
        interaction: discord.Interaction,
        faction: app_commands.Choice[str],
        account: str,
        proposal: str,
        budget: app_commands.Range[int, 1, 100_000_000],
    ):
        """Open a direct negotiation with Gilbert (Union) or Albert (Crown).

        There is no dice roll and no membership requirement: the account's
        leader (or an admin) may request from either faction, and the numbers
        are already in the representative's briefing. The proposal is a free
        text the representative reads; there are no item/project fields.
        """
        if await self._guild_only(interaction):
            return
        atype, row = await self._account(interaction, account)
        if row is None:
            return
        if not self._can_act(interaction, row):
            return await self._not_authorized(interaction, atype, row)

        proposal = (proposal or "").strip()
        if not proposal:
            return await self._faction_error(
                interaction,
                "Proposal Needed",
                "Write a short proposal: what you will provide, how much, and when.",
            )

        contract_cog = self.bot.get_cog("Contract")
        if contract_cog is None:
            return await self._faction_error(
                interaction,
                "Contract Desk Unavailable",
                "The contract desk is not loaded.",
            )
        await contract_cog.request_contract(
            interaction, account, proposal, int(budget), faction=faction.value
        )

    @faction_group.command(name="commitment", description="Request a Union whole-balance commitment.")
    @app_commands.describe(account="The nation or kingdom committing its balance.")
    @app_commands.autocomplete(account=account_autocomplete)
    async def faction_commitment(self, interaction: discord.Interaction, account: str):
        union = self.bot.get_cog("Union")
        if union is None:
            return await self._faction_error(
                interaction, "Union Unavailable", "The Union desk is not loaded."
            )
        await union.request_commit(interaction, account)

    @faction_group.command(name="uncommit", description="Stop a Union whole-balance commitment.")
    @app_commands.describe(account="The nation or kingdom stopping its commitment.")
    @app_commands.autocomplete(account=account_autocomplete)
    async def faction_uncommit(self, interaction: discord.Interaction, account: str):
        union = self.bot.get_cog("Union")
        if union is None:
            return await self._faction_error(
                interaction, "Union Unavailable", "The Union desk is not loaded."
            )
        await union.cancel_commit(interaction, account)

    @faction_group.command(name="donate", description="Donate credits to the account's faction.")
    @app_commands.describe(
        faction="Which faction the account serves.",
        account="The nation or kingdom donating.",
        amount="Amount, or all / half / quarter of the account balance.",
    )
    @app_commands.choices(faction=[
        app_commands.Choice(name="⚒️ Workers' Union", value=factions.UNION),
        app_commands.Choice(name="👑 The Crown", value=factions.CROWN),
    ])
    @app_commands.autocomplete(account=account_autocomplete)
    async def faction_donate(
        self, interaction: discord.Interaction, faction: app_commands.Choice[str], account: str, amount: str
    ):
        if not await self._require_faction_member(interaction, account, faction.value):
            return
        await self.donate.callback(self, interaction, account, amount, None, None)

    @faction_group.command(name="deliver", description="Deliver catalog items to a Crown contract.")
    @app_commands.describe(
        account="The Crown nation or kingdom delivering.",
        item="The catalog item to deliver.",
        quantity="Units to deliver.",
    )
    @app_commands.autocomplete(account=account_autocomplete, item=item_autocomplete)
    async def faction_deliver(self, interaction: discord.Interaction, account: str, item: str, quantity: int):
        if not await self._require_faction_member(interaction, account, factions.CROWN):
            return
        await self.donate.callback(self, interaction, account, None, item, quantity)

    @faction_group.command(name="status", description="Show a faction or account ledger.")
    @app_commands.describe(
        faction="Which faction's ledger to show.",
        account="Optional nation/kingdom for the Crown record.",
    )
    @app_commands.choices(faction=[
        app_commands.Choice(name="⚒️ Workers' Union", value=factions.UNION),
        app_commands.Choice(name="👑 The Crown", value=factions.CROWN),
    ])
    @app_commands.autocomplete(account=account_autocomplete)
    async def faction_status(
        self, interaction: discord.Interaction, faction: app_commands.Choice[str], account: Optional[str] = None
    ):
        if faction.value == factions.CROWN:
            return await self.crown_status.callback(self, interaction, account)
        union = self.bot.get_cog("Union")
        if union is None:
            return await self._faction_error(interaction, "Union Unavailable", "The Union desk is not loaded.")
        await union.balance.callback(union, interaction)

    @faction_group.command(name="contracts", description="List faction contracts.")
    @app_commands.describe(
        faction="Which faction's contracts to show.",
        account="Optional nation or kingdom to filter by.",
    )
    @app_commands.choices(faction=[
        app_commands.Choice(name="⚒️ Workers' Union", value=factions.UNION),
        app_commands.Choice(name="👑 The Crown", value=factions.CROWN),
    ])
    @app_commands.autocomplete(account=account_autocomplete)
    async def faction_contracts(
        self, interaction: discord.Interaction, faction: app_commands.Choice[str], account: Optional[str] = None
    ):
        contract = self.bot.get_cog("Contract")
        if contract is None:
            return await self._faction_error(interaction, "Contract Desk Unavailable", "The contract desk is not loaded.")
        await contract.list_for_faction(interaction, faction.value, account)

    @faction_group.command(name="cancel", description="Cancel an active Crown supplier contract.")
    @app_commands.describe(contract_id="The Crown contract id.")
    async def faction_cancel(self, interaction: discord.Interaction, contract_id: int):
        await self.crown_cancel.callback(self, interaction, int(contract_id))

    @faction_group.command(name="fund", description="[Admin] Add credits to a faction treasury.")
    @app_commands.describe(faction="Which treasury to fund.", amount="Credits to add.")
    @app_commands.choices(faction=[
        app_commands.Choice(name="⚒️ Workers' Union", value=factions.UNION),
        app_commands.Choice(name="👑 The Crown", value=factions.CROWN),
    ])
    @admin_only()
    async def faction_fund(
        self, interaction: discord.Interaction, faction: app_commands.Choice[str], amount: app_commands.Range[int, 1, None]
    ):
        # The delegate is an @admin_only command, but calling its ``.callback``
        # skips that check, so the gate has to be repeated here.
        if not await self._require_admin(interaction, "fund a faction treasury"):
            return
        if faction.value == factions.CROWN:
            return await self.crown_fund.callback(self, interaction, amount)
        union = self.bot.get_cog("Union")
        if union is None:
            return await self._faction_error(interaction, "Union Unavailable", "The Union desk is not loaded.")
        await union.fund.callback(union, interaction, amount)

    @faction_group.command(name="withdraw", description="[Admin] Withdraw from the Crown treasury.")
    @app_commands.describe(amount="Credits to withdraw.")
    @admin_only()
    async def faction_withdraw(self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, None]):
        if not await self._require_admin(interaction, "withdraw from the Crown treasury"):
            return
        await self.crown_withdraw.callback(self, interaction, amount)

    @faction_group.command(name="members", description="List Workers' Union members.")
    async def faction_members(self, interaction: discord.Interaction):
        union = self.bot.get_cog("Union")
        if union is None:
            return await self._faction_error(interaction, "Union Unavailable", "The Union desk is not loaded.")
        await union.members.callback(union, interaction)

    @faction_group.command(name="setchannel", description="[Admin] Set the Union commitment channel.")
    @app_commands.describe(channel="The channel admins will watch for commitment requests.")
    @admin_only()
    async def faction_setchannel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if not await self._require_admin(interaction, "set the Union commitment channel"):
            return
        union = self.bot.get_cog("Union")
        if union is None:
            return await self._faction_error(interaction, "Union Unavailable", "The Union desk is not loaded.")
        await union.setchannel.callback(union, interaction, channel)

    # ------------------------------------------------------------------
    # /join
    # ------------------------------------------------------------------
    @app_commands.command(
        name="join",
        description="Join the Workers' Union or The Crown as a nation/kingdom.",
    )
    @app_commands.describe(
        faction="Which faction to serve.",
        account="The nation or kingdom joining.",
    )
    @app_commands.choices(
        faction=[
            app_commands.Choice(name="⚒️ Workers' Union (Gilbert)", value=factions.UNION),
            app_commands.Choice(name="👑 The Crown (Albert)", value=factions.CROWN),
        ]
    )
    @app_commands.autocomplete(account=account_autocomplete)
    async def join(
        self,
        interaction: discord.Interaction,
        faction: app_commands.Choice[str],
        account: str,
    ):
        if await self._guild_only(interaction):
            return
        atype, row = await self._account(interaction, account)
        if row is None:
            return
        if not self._can_act(interaction, row):
            return await self._not_authorized(interaction, atype, row)

        try:
            result = await factions.join_faction(
                self.db,
                guild_id=interaction.guild_id,
                account_type=atype,
                account_id=row["id"],
                faction=faction.value,
                actor_id=interaction.user.id,
            )
        except factions.FactionError as exc:
            return await interaction.response.send_message(
                embed=embeds.error("Allegiance Refused", str(exc)), ephemeral=True
            )

        label = factions.faction_label(result.faction)
        if result.already:
            detail = f"{account_emoji(atype)} **{row['name']}** already serves **{label}**."
            return await interaction.response.send_message(
                embed=embeds.info("Already a Member", detail),
                ephemeral=True,
            )

        lines = [f"{account_emoji(atype)} **{row['name']}** now serves **{label}**."]
        if result.previous:
            lines.append(f"Left **{factions.faction_label(result.previous)}**.")
        if result.reactivated:
            lines.append("Membership reactivated on the existing record.")
        if result.cancelled_commit:
            lines.append("The old Union commitment was cancelled.")
        if result.cancelled_contracts:
            lines.append(
                f"**{result.cancelled_contracts}** active Crown contract(s) were cancelled."
            )
        if result.faction == factions.CROWN:
            grace = factions.grace_ends_at(result.joined_at)
            if grace:
                lines.append(
                    f"Crown tax grace ends <t:{int(grace.timestamp())}:R>; "
                    "after that the regular-soldier line is taxed 100%."
                )
        await interaction.response.send_message(
            embed=embeds.success("Allegiance Sworn", "\n".join(lines))
        )


    # ------------------------------------------------------------------
    # /donate · credits or Crown items
    # ------------------------------------------------------------------
    @app_commands.command(
        name="donate",
        description="Donate credits to your faction, or deliver items to The Crown.",
    )
    @app_commands.describe(
        account="The nation/kingdom donating.",
        amount="Credits to donate (or all / half / quarter). Use this or item.",
        item="Crown contract item to deliver. Use this or amount.",
        quantity="Units of the item to deliver.",
    )
    @app_commands.autocomplete(account=account_autocomplete, item=item_autocomplete)
    async def donate(
        self,
        interaction: discord.Interaction,
        account: str,
        amount: Optional[str] = None,
        item: Optional[str] = None,
        quantity: Optional[int] = None,
    ):
        if await self._guild_only(interaction):
            return
        atype, row = await self._account(interaction, account)
        if row is None:
            return
        if not self._can_act(interaction, row):
            return await self._not_authorized(interaction, atype, row)

        if bool(amount) == bool(item):
            return await interaction.response.send_message(
                embed=embeds.error(
                    "Choose One",
                    "Donate either credits with `amount` or Crown items with "
                    "`item` + `quantity` — not both in one command.",
                ),
                ephemeral=True,
            )
        if amount and quantity is not None:
            return await interaction.response.send_message(
                embed=embeds.error(
                    "Choose One",
                    "`quantity` belongs to an item delivery, not a credit donation.",
                ),
                ephemeral=True,
            )

        if amount:
            member = await self.db.get_faction_member(
                interaction.guild_id, atype, row["id"]
            )
            if member is None:
                return await interaction.response.send_message(
                    embed=embeds.error(
                        "No Faction",
                        f"{account_emoji(atype)} **{row['name']}** has not joined "
                        "a faction. Use `/faction join` first.",
                    ),
                    ephemeral=True,
                )
            try:
                amount_int = parse_amount(amount, int(row["balance"]))
            except ValueError as exc:
                return await interaction.response.send_message(
                    embed=embeds.error("Invalid Amount", str(exc)), ephemeral=True
                )
            try:
                result = await factions.donate_credits(
                    self.db,
                    guild_id=interaction.guild_id,
                    account_type=atype,
                    account_id=row["id"],
                    faction=member["faction"],
                    amount=amount_int,
                    actor_id=interaction.user.id,
                )
            except factions.FactionError as exc:
                return await interaction.response.send_message(
                    embed=embeds.error("Donation Refused", str(exc)), ephemeral=True
                )
            except InsufficientFunds as exc:
                return await interaction.response.send_message(
                    embed=embeds.error(
                        "Insufficient Funds",
                        f"{account_emoji(atype)} **{row['name']}** only holds "
                        f"**{money(exc.balance)}**.",
                    ),
                    ephemeral=True,
                )
            return await interaction.response.send_message(
                embed=embeds.success(
                    "Donation Accepted",
                    f"{account_emoji(atype)} **{row['name']}** donated "
                    f"**{money(result.amount)}** to "
                    f"**{factions.faction_label(member['faction'])}**.\n"
                    f"Treasury: **{money(result.treasury_after)}** · "
                    f"Account balance: **{money(result.balance_after)}**",
                )
            )

        if quantity is None or int(quantity) <= 0:
            return await interaction.response.send_message(
                embed=embeds.error("Quantity", "Give a positive quantity to deliver."),
                ephemeral=True,
            )
        try:
            delivery = await factions.deliver_crown_items(
                self.db,
                guild_id=interaction.guild_id,
                account_type=atype,
                account_id=row["id"],
                item_key=item,
                quantity=int(quantity),
                actor_id=interaction.user.id,
            )
        except factions.CrownTreasuryShort as exc:
            return await interaction.response.send_message(
                embed=embeds.error("Crown Treasury Short", str(exc)), ephemeral=True
            )
        except factions.FactionError as exc:
            return await interaction.response.send_message(
                embed=embeds.error("Delivery Refused", str(exc)), ephemeral=True
            )

        if delivery.fulfilled:
            head = (
                f"**{delivery.paid:,} credits** paid for "
                f"**{len(delivery.fulfilled)}** completed contract(s)."
            )
        elif delivery.paid:
            head = f"**{money(delivery.paid)}** paid."
        else:
            head = "No contract completed; the delivery was recorded as a donation."
        extra = (
            f"\nExcess beyond active contracts: **{delivery.excess}** unit(s)."
            if delivery.excess
            else ""
        )
        await interaction.response.send_message(
            embed=embeds.success(
                "Crown Delivery",
                f"{account_emoji(atype)} **{row['name']}** delivered "
                f"**{delivery.delivered}** unit(s).\n{head}{extra}\n"
                f"Crown treasury: **{money(delivery.treasury_after)}**",
            )
        )


    @group.command(
        name="status",
        description="Albert's report on a nation/kingdom, or the Crown treasury.",
    )
    @app_commands.describe(account="Optional nation/kingdom to inspect.")
    @app_commands.autocomplete(account=account_autocomplete)
    async def crown_status(
        self, interaction: discord.Interaction, account: Optional[str] = None
    ):
        if await self._guild_only(interaction):
            return
        if account:
            atype, row = await self._account(interaction, account)
            if row is None:
                return
            report = await factions.crown_report(
                self.db,
                guild_id=interaction.guild_id,
                account_type=atype,
                account_id=row["id"],
            )
            return await interaction.response.send_message(
                embed=self._status_embed(report, atype, row["name"])
            )

        await factions.expire_crown_contracts(
            self.db,
            interaction.guild_id,
            await self.db.current_cycle(interaction.guild_id),
        )
        balance = await self.db.get_crown_balance(interaction.guild_id)
        reserved = await self.db.crown_reserved(interaction.guild_id)
        members = await self.db.list_faction_members(
            interaction.guild_id, faction=factions.CROWN
        )
        active = await self.db.list_crown_contracts(
            interaction.guild_id, status="active"
        )
        embed = embeds.info(
            "👑 The Crown · Royal Orders & Court",
            "Albert keeps the Crown's ledger. Ask about a specific nation or "
            "kingdom with `/faction status`.",
        )
        embed.add_field(
            name="Crown Treasury", value=f"## {money(balance)}", inline=False
        )
        embed.add_field(
            name="Reserved for Contracts", value=money(reserved), inline=True
        )
        embed.add_field(name="Crown Members", value=str(len(members)), inline=True)
        embed.add_field(name="Active Contracts", value=str(len(active)), inline=True)
        embed.set_footer(text="Albert · Royal Orders & Court")
        await interaction.response.send_message(embed=embed)

    @group.command(
        name="contracts",
        description="List a nation/kingdom's Crown supplier contracts.",
    )
    @app_commands.describe(account="The nation or kingdom.")
    @app_commands.autocomplete(account=account_autocomplete)
    async def crown_contracts(self, interaction: discord.Interaction, account: str):
        if await self._guild_only(interaction):
            return
        atype, row = await self._account(interaction, account)
        if row is None:
            return
        await factions.expire_crown_contracts(
            self.db,
            interaction.guild_id,
            await self.db.current_cycle(interaction.guild_id),
        )
        contracts = await self.db.list_crown_contracts(
            interaction.guild_id, account_type=atype, account_id=row["id"], limit=15
        )
        if not contracts:
            return await interaction.response.send_message(
                embed=embeds.info(
                    "No Crown Contracts",
                    f"{account_emoji(atype)} **{row['name']}** has never held a "
                    "Crown supplier contract.",
                )
            )
        lines = []
        for contract in contracts:
            lines.append(
                f"**#{contract['id']}** {contract['display_name']} — "
                f"{contract['delivered']}/{contract['quantity']} delivered · "
                f"{money(contract['value'])} · **{contract['status']}** · "
                f"deadline c{contract['deadline_cycle']}"
            )
        embed = embeds.info(
            f"👑 Crown Contracts · {row['name']}",
            "\n".join(lines)[:4096],
        )
        embed.set_footer(
            text="Albert · Royal Orders & Court · cancel with /faction cancel"
        )
        await interaction.response.send_message(embed=embed)

    @group.command(
        name="cancel",
        description="Cancel an active Crown supplier contract.",
    )
    @app_commands.describe(contract_id="The contract number from /faction contracts.")
    async def crown_cancel(
        self, interaction: discord.Interaction, contract_id: int
    ):
        if await self._guild_only(interaction):
            return
        contract = await self.db.get_crown_contract(int(contract_id))
        if contract is None or int(contract["guild_id"]) != int(interaction.guild_id):
            return await interaction.response.send_message(
                embed=embeds.error("Not Found", "No Crown contract matches that id."),
                ephemeral=True,
            )
        atype = contract["account_type"]
        row = await factions.get_account(
            self.db, interaction.guild_id, atype, int(contract["account_id"])
        )
        if row is None:
            return await interaction.response.send_message(
                embed=embeds.error("Account Gone", "The contracting account no longer exists."),
                ephemeral=True,
            )
        if not self._can_act(interaction, row):
            return await self._not_authorized(interaction, atype, row)
        try:
            contract = await factions.cancel_crown_contract(
                self.db,
                guild_id=interaction.guild_id,
                contract_id=int(contract_id),
                actor_id=interaction.user.id,
            )
        except factions.FactionError as exc:
            return await interaction.response.send_message(
                embed=embeds.error("Cannot Cancel", str(exc)), ephemeral=True
            )
        await interaction.response.send_message(
            embed=embeds.success(
                "Contract Cancelled",
                f"Crown contract **#{contract['id']}** "
                f"({contract['display_name']}) was cancelled. No payment moved; "
                "any delivered units are forfeited.",
            )
        )


    @group.command(
        name="fund",
        description="[Admin] Credit the Crown treasury so it can pay suppliers.",
    )
    @app_commands.describe(amount="Credits to add to the Crown.")
    @admin_only()
    async def crown_fund(
        self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, None]
    ):
        if await self._guild_only(interaction):
            return
        async with self.db.transaction():
            after = await self.db.credit_crown(interaction.guild_id, amount)
            await self.db.log_tx(
                guild_id=interaction.guild_id,
                kind="crown_fund",
                account_type="crown",
                account_id=interaction.guild_id,
                account_name="The Crown",
                amount=amount,
                balance_after=after,
                actor_id=interaction.user.id,
                note="Admin funded The Crown",
            )
        await interaction.response.send_message(
            embed=embeds.success(
                "Crown Funded",
                f"**{money(amount)}** added to The Crown.\n"
                f"New balance: **{money(after)}**",
            )
        )

    @group.command(
        name="withdraw",
        description="[Admin] Remove unreserved credits from the Crown treasury.",
    )
    @app_commands.describe(amount="Credits to take out of the Crown.")
    @admin_only()
    async def crown_withdraw(
        self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, None]
    ):
        if await self._guild_only(interaction):
            return
        await factions.expire_crown_contracts(
            self.db,
            interaction.guild_id,
            await self.db.current_cycle(interaction.guild_id),
        )
        problem = None
        after = 0
        async with self.db.transaction():
            balance = await self.db.get_crown_balance(interaction.guild_id)
            reserved = await self.db.crown_reserved(interaction.guild_id)
            if balance - amount < reserved:
                problem = (
                    f"The Crown holds **{money(balance)}** and has "
                    f"**{money(reserved)}** reserved for active supplier contracts. "
                    f"Only **{money(max(0, balance - reserved))}** is free."
                )
            else:
                after = await self.db.debit_crown(interaction.guild_id, amount)
                await self.db.log_tx(
                    guild_id=interaction.guild_id,
                    kind="crown_withdraw",
                    account_type="crown",
                    account_id=interaction.guild_id,
                    account_name="The Crown",
                    amount=-amount,
                    balance_after=after,
                    actor_id=interaction.user.id,
                    note="Admin withdrew from The Crown",
                )
        if problem:
            return await interaction.response.send_message(
                embed=embeds.error("Crown Funds Reserved", problem), ephemeral=True
            )
        await interaction.response.send_message(
            embed=embeds.success(
                "Crown Withdrawn",
                f"**{money(amount)}** taken out of The Crown.\n"
                f"New balance: **{money(after)}**",
            )
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Factions(bot))
    # /faction is the only faction entry point. Some old commands remain as
    # callable implementation objects for the hub, but leave the command tree
    # so users cannot reach the separate surface.
    for name in ("join", "donate", "audience", "crown", "union"):
        bot.tree.remove_command(name)

