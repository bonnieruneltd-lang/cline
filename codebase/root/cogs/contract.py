"""
cogs/contract.py
================
Faction contract system — Gilbert for the Workers' Union, Albert for The Crown.

Flow
----
1. ``/faction contract faction:<union|crown>`` opens a private negotiation
   thread and mints a case ID (``UN-<TAG>-<NNN>`` for the Union,
   ``CR-<TAG>-<NNN>`` for the Crown). Quota per *natural* payout cycle, per
   faction: 1 for a nation, 3 for a kingdom. Membership is never required.
2. Every human message in the thread is answered by that faction's persona.
   The briefing already carries the account's live numbers, so the
   representative knows them from the first reply.
3. ``/contract close`` (or the Close Ticket button) archives the ticket.
4. ``/contract approve`` records the 25/75 split and the claim account. It
   requires the funding faction's treasury to hold the whole budget. Nothing is
   debited from the party and no credits move yet.
5. ``/contract claim`` pays the 25% tranche out of the funding treasury.
6. ``/contract release`` (after inspection) unlocks the 75% tranche for claim.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import albert_prompt
import config
import embeds
import factions as faction_rules
from checks import admin_only
from contract_limits import budget_allowed, limit_explanation, max_budget_for_funds
from database import InsufficientFunds
from gilbert.ai import GilbertAI
from gilbert.prompt import GILBERT_SYSTEM_PROMPT, classify_tier, contract_context
from utils import account_autocomplete, account_emoji, money, parse_account


STATUS_LABELS = {
    "negotiating": "🟡 Negotiating",
    "approved": "🟢 Approved",
    "completed": "✅ Completed",
    "rejected": "🔴 Rejected",
    "cancelled": "⚪ Cancelled",
    "closed": "🔒 Closed",
}


class ClaimError(Exception):
    """Raised when a contract's credits cannot be paid out."""


class ApprovalError(Exception):
    """Raised when a contract cannot be approved, with a user-facing reason."""

    def __init__(self, title: str, detail: str) -> None:
        super().__init__(detail)
        self.title = title
        self.detail = detail


def _nation_tag(name: str) -> str:
    tag = re.sub(r"[^A-Za-z0-9]", "", name).upper()
    return (tag or "NAT")[:6]


def _thread_name(name: str, faction: str = "union") -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-") or "nation"
    prefix = "crown" if faction == faction_rules.CROWN else "union"
    return f"{prefix}-contract-{slug}"[:100]


def _member_is_admin(member: discord.abc.User) -> bool:
    perms = getattr(member, "guild_permissions", None)
    return bool(perms and perms.administrator)


class ContractCloseView(discord.ui.View):
    """Persistent Close Ticket button attached to every negotiation."""

    def __init__(self, cog: "Contract", contract_id: int) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.contract_id = contract_id
        button = discord.ui.Button(
            label="Close Ticket",
            emoji="🔒",
            style=discord.ButtonStyle.danger,
            custom_id=f"contract_close:{contract_id}",
        )
        button.callback = self._on_close
        self.add_item(button)

    async def _on_close(self, interaction: discord.Interaction) -> None:
        await self.cog.close_contract(interaction, self.contract_id)


class ContractClaimView(discord.ui.View):
    """Persistent Claim Credits button posted to the contract board."""

    def __init__(self, cog: "Contract", contract_id: int) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.contract_id = contract_id
        button = discord.ui.Button(
            label="Claim Credits",
            emoji="💰",
            style=discord.ButtonStyle.success,
            custom_id=f"contract_claim:{contract_id}",
        )
        button.callback = self._on_claim
        self.add_item(button)

    async def _on_claim(self, interaction: discord.Interaction) -> None:
        await self.cog.claim_from_board(interaction, self.contract_id)


class Contract(commands.Cog):
    """🛠️ Faction contracts, negotiated by Gilbert and Albert."""

    # A rejected or cancelled case is final. Reopening one would move it back to
    # negotiating and make its forfeited tranches claimable again, so those
    # statuses are deliberately absent. checks/check_contracts.py pins this.
    REOPENABLE_STATUSES = ("approved", "closed", "completed")

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.ai = GilbertAI()
        self._thread_ids: set[int] = set()
        self._locks: dict[int, asyncio.Lock] = {}

    @property
    def db(self):
        return self.bot.db  # type: ignore[attr-defined]

    group = app_commands.Group(name="contract", description="🛠️ Faction contracts, negotiated by Gilbert and Albert.")

    async def cog_load(self) -> None:
        """Re-register persistent Close and Claim buttons and warm the thread cache."""
        try:
            rows = await self.db.fetchall(
                """SELECT id, thread_id FROM contracts
                   WHERE status IN ('negotiating', 'approved') AND thread_id IS NOT NULL"""
            )
            for row in rows:
                self._thread_ids.add(int(row["thread_id"]))
                self.bot.add_view(ContractCloseView(self, int(row["id"])))
        except Exception:  # pragma: no cover - defensive
            self.bot.logger.exception("Failed to restore contract views.")

        try:
            for row in await self.db.contracts_with_board_messages():
                self.bot.add_view(ContractClaimView(self, int(row["id"])))
        except Exception:  # pragma: no cover - defensive
            self.bot.logger.exception("Failed to restore contract claim views.")

    # ------------------------------------------------------------------
    # Permission helpers
    # ------------------------------------------------------------------
    def _is_union_admin(self, member: discord.abc.User, settings) -> bool:
        if _member_is_admin(member):
            return True
        role_id = settings["union_admin_role_id"] if settings else None
        return bool(role_id) and any(r.id == role_id for r in getattr(member, "roles", []))

    @staticmethod
    def _same_guild(interaction: discord.Interaction, contract) -> bool:
        """A contract must only be touched from the guild that owns it."""
        return (
            interaction.guild_id is not None
            and int(contract["guild_id"]) == int(interaction.guild_id)
        )

    async def _wrong_guild(self, interaction: discord.Interaction) -> None:
        embed = embeds.error("Wrong Server", "That contract belongs to another server.")
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)
    def _can_speak(self, contract, member: discord.abc.User, settings) -> bool:
        return member.id == contract["requester_id"] or self._is_union_admin(member, settings)

    async def _get_channel(self, guild: discord.Guild, channel_id: Optional[int]):
        if not channel_id:
            return None
        channel = guild.get_channel(channel_id)
        if channel is not None:
            return channel
        try:
            return await self.bot.fetch_channel(channel_id)
        except Exception:
            return None

    async def _get_member(self, guild: discord.Guild, member_id: int):
        member = guild.get_member(member_id)
        if member is not None:
            return member
        try:
            return await guild.fetch_member(member_id)
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Contract owner (nation or kingdom) helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _owner_type(contract) -> str:
        try:
            owner_type = contract["owner_type"]
        except (KeyError, IndexError):
            owner_type = None
        return owner_type or "nation"

    @staticmethod
    def _contract_faction(contract) -> str:
        """The faction whose treasury funds this case (legacy rows: union)."""
        try:
            value = contract["faction"]
        except (KeyError, IndexError, TypeError):
            value = None
        return value or faction_rules.UNION

    def _contract_label(self, contract) -> str:
        return "Crown" if self._contract_faction(contract) == faction_rules.CROWN else "Union"

    def _agent_name(self, contract) -> str:
        return "Albert" if self._contract_faction(contract) == faction_rules.CROWN else "Gilbert"

    def _owner_id(self, contract) -> int:
        try:
            owner_id = contract["owner_id"]
        except (KeyError, IndexError):
            owner_id = None
        return int(owner_id) if owner_id is not None else int(contract["nation_id"])

    async def _owner_row(self, contract):
        if self._owner_type(contract) == "kingdom":
            row = await self.db.get_kingdom_by_id(self._owner_id(contract))
        else:
            row = await self.db.get_nation_by_id(self._owner_id(contract))
        if row is None or int(row["guild_id"]) != int(contract["guild_id"]):
            return None
        return row

    async def _resolve_owner(self, guild_id: int, value: str):
        owner_type, name = parse_account(value)
        if owner_type is None:
            owner_type, name = "nation", (value or "").strip()
        if owner_type == "kingdom":
            return owner_type, await self.db.get_kingdom(guild_id, name)
        return owner_type, await self.db.get_nation(guild_id, name)

    # ------------------------------------------------------------------
    # Embeds
    # ------------------------------------------------------------------
    def _contract_embed(
        self,
        contract,
        *,
        detailed: bool = False,
        claim_name: Optional[str] = None,
    ) -> discord.Embed:
        status = STATUS_LABELS.get(contract["status"], contract["status"].title())
        label = self._contract_label(contract)
        icon = "👑" if label == "Crown" else "🛠️"
        embed = discord.Embed(
            title=f"{icon} {label} Contract · {contract['case_id']}",
            color=config.GOLD_COLOR,
        )
        embed.add_field(
            name="Kingdom" if self._owner_type(contract) == "kingdom" else "Nation",
            value=contract["nation_name"],
            inline=True,
        )
        embed.add_field(name="Requested By", value=f"<@{contract['requester_id']}>", inline=True)
        embed.add_field(name="Natural Cycle", value=str(contract["cycle"]), inline=True)
        embed.add_field(name="Budget", value=money(contract["budget"]), inline=True)
        embed.add_field(name="Tier", value=classify_tier(contract["budget"]), inline=True)
        embed.add_field(name="Status", value=status, inline=True)
        embed.add_field(name="Funded By", value="👑 The Crown" if label == "Crown" else "⚒️ Workers' Union", inline=True)
        embed.add_field(name="Proposal", value=(contract["project"] or "_not stated_")[:1024], inline=False)
        if detailed:
            legacy = bool(contract["redeem_key"])
            approved = contract["approved_at"] is not None
            if contract["upfront_claimed"]:
                upfront_state = "✅ claimed"
            elif legacy:
                upfront_state = "🎟 in-game key issued"
            elif approved:
                upfront_state = "🔓 claimable"
            else:
                upfront_state = "⏳ awaiting approval"
            if contract["final_claimed"]:
                escrow_state = "✅ claimed"
            elif contract["released"] > 0:
                escrow_state = "🔓 claimable"
            else:
                escrow_state = "🔒 held until release"
            embed.add_field(
                name="Upfront (25%)",
                value=f"{money(contract['upfront'])}\n{upfront_state}",
                inline=True,
            )
            embed.add_field(
                name="Escrow (75%)",
                value=f"{money(contract['escrow'])}\n{escrow_state}",
                inline=True,
            )
            embed.add_field(name="Released", value=money(contract["released"]), inline=True)
            if claim_name:
                claim_type = contract["claim_account_type"] or "nation"
                embed.add_field(
                    name="Claim Account",
                    value=f"{account_emoji(claim_type)} **{claim_name}**",
                    inline=False,
                )
            if contract["redeem_key"]:
                embed.add_field(name="🎟 Upfront Key", value=f"```{contract['redeem_key']}```", inline=False)
            if contract["final_key"] and contract["status"] == "completed":
                embed.add_field(name="🎟 Final Key", value=f"```{contract['final_key']}```", inline=False)
        embed.set_footer(
            text="⚜ The Realm of Rune · The Crown" if label == "Crown"
            else "⚜ The Realm of Rune · Workers' Union"
        )
        return embed

    # ------------------------------------------------------------------
    # Negotiation space creation
    # ------------------------------------------------------------------
    async def _create_negotiation_space(
        self,
        interaction: discord.Interaction,
        nation_row,
        case_id: str,
        settings,
        faction: str = "union",
    ):
        guild = interaction.guild
        parent = await self._get_channel(guild, settings["contract_channel_id"])
        if parent is None:
            raise RuntimeError("The configured contract channel no longer exists.")

        label = "The Crown" if faction == faction_rules.CROWN else "Workers' Union"
        name = _thread_name(nation_row["name"], faction)
        reason = f"{label} contract {case_id}"

        try:
            thread = await parent.create_thread(
                name=name,
                type=discord.ChannelType.private_thread,
                invitable=False,
                auto_archive_duration=config.CONTRACT_THREAD_ARCHIVE_MINUTES,
                reason=reason,
            )
            await thread.add_user(interaction.user)
            await self._add_union_admins(thread, guild, settings)
            return thread
        except (discord.Forbidden, discord.HTTPException, AttributeError):
            # Fall back to a private text channel under the same category.
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                guild.me: discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, manage_channels=True
                ),
                interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True),
            }
            role_id = settings["union_admin_role_id"]
            if role_id:
                role = guild.get_role(role_id)
                if role:
                    overwrites[role] = discord.PermissionOverwrite(
                        view_channel=True, send_messages=True
                    )
            return await guild.create_text_channel(
                name=name, category=parent.category, overwrites=overwrites, reason=reason
            )

    async def _add_union_admins(self, thread: discord.Thread, guild: discord.Guild, settings) -> None:
        role_id = settings["union_admin_role_id"]
        if not role_id:
            return
        role = guild.get_role(role_id)
        if role is None:
            return
        for member in list(role.members)[:50]:
            try:
                await thread.add_user(member)
            except discord.HTTPException:
                pass

    # ------------------------------------------------------------------
    # Gilbert generation
    # ------------------------------------------------------------------
    async def _send_chunks(self, channel, text: str) -> None:
        for i in range(0, len(text), 1900):
            await channel.send(text[i : i + 1900])

    async def _crown_contract_context(
        self, contract, nation, requester_name: str, owner_type: str
    ) -> str:
        """Build Albert's ground-truth briefing, numbers included."""
        guild_id = contract["guild_id"]
        owner_id = self._owner_id(contract)
        member = await self.db.get_faction_member(guild_id, owner_type, owner_id)
        soldiers = await faction_rules.account_soldiers(
            self.db, guild_id, owner_type, owner_id, nation
        )
        donations_row = await self.db.fetchone(
            """SELECT COALESCE(SUM(amount), 0) AS total
               FROM faction_donations
               WHERE guild_id = ? AND account_type = ? AND account_id = ?
                 AND faction = 'crown'""",
            (guild_id, owner_type, owner_id),
        )
        supply_row = await self.db.fetchone(
            """SELECT COALESCE(SUM(units_sold), 0) AS units
               FROM crown_sales
               WHERE guild_id = ? AND account_type = ? AND account_id = ?""",
            (guild_id, owner_type, owner_id),
        )
        faction = member["faction"] if member else None
        status = member["status"] if member else None
        joined_at = member["joined_at"] if member else None
        tax_active, tax_reason = faction_rules.tax_status(
            faction=faction, status=status, joined_at=joined_at, soldiers=soldiers
        )
        budget = int(contract["budget"])
        return albert_prompt.contract_context(
            case_id=contract["case_id"],
            owner_type=owner_type,
            account_name=contract["nation_name"],
            requester_name=requester_name,
            budget=budget,
            proposal=contract["project"] or "(not stated)",
            tier=classify_tier(budget),
            cycle=int(contract["cycle"]),
            account_balance=int(nation["balance"]) if nation else 0,
            crown_balance=await self.db.get_crown_balance(guild_id),
            crown_reserved=await self.db.crown_reserved(guild_id),
            contracts_used=await self.db.count_contracts_for_cycle(
                guild_id, owner_type, owner_id, contract["cycle"],
                faction=faction_rules.CROWN,
            ),
            contract_limit=config.CONTRACTS_PER_CYCLE.get(owner_type, 1),
            soldiers=soldiers,
            faction=faction,
            status=status,
            donations=int(donations_row["total"]) if donations_row else 0,
            supply_units=int(supply_row["units"]) if supply_row else 0,
            tax_active=tax_active,
            tax_reason=tax_reason,
        )

    async def _agent_reply(self, contract, channel, *, user_text: Optional[str] = None) -> None:
        history: list[dict] = json.loads(contract["history"] or "[]")
        if user_text:
            history.append({"role": "user", "content": user_text})

        nation = await self._owner_row(contract)
        member = await self._get_member(channel.guild, contract["requester_id"])
        requester_name = member.display_name if member else f"user {contract['requester_id']}"

        owner_type = self._owner_type(contract)
        faction = self._contract_faction(contract)
        if faction == faction_rules.CROWN:
            context = await self._crown_contract_context(
                contract, nation, requester_name, owner_type
            )
            system_prompt = albert_prompt.ALBERT_SYSTEM_PROMPT
        else:
            contracts_used = await self.db.count_contracts_for_cycle(
                contract["guild_id"], owner_type, self._owner_id(contract),
                contract["cycle"], faction=faction_rules.UNION,
            )
            union_contributions = await self.db.union_contributions(
                contract["guild_id"], owner_type, self._owner_id(contract)
            )
            soldiers = await faction_rules.account_soldiers(
                self.db, contract["guild_id"], owner_type,
                self._owner_id(contract), nation,
            )
            context = contract_context(
                case_id=contract["case_id"],
                owner_type=owner_type,
                nation_name=contract["nation_name"],
                requester_name=requester_name,
                budget=contract["budget"],
                proposal=contract["project"] or "(not stated)",
                cycle=contract["cycle"],
                nation_balance=int(nation["balance"]) if nation else 0,
                max_budget=max_budget_for_funds(int(nation["balance"])) if nation else 0,
                status=contract["status"],
                union_balance=await self.db.get_union_balance(contract["guild_id"]),
                contracts_used=contracts_used,
                contract_limit=config.CONTRACTS_PER_CYCLE.get(owner_type, 1),
                union_member=await self.db.is_union_member(
                    contract["guild_id"], owner_type, self._owner_id(contract)
                ),
                union_dues_paid=union_contributions["dues"],
                union_donated=union_contributions["donations"],
                union_committed=union_contributions["committed"],
                soldiers=soldiers,
            )
            system_prompt = GILBERT_SYSTEM_PROMPT

        messages = [{"role": "system", "content": system_prompt + "\n\n" + context}]
        messages.extend(history[-config.CONTRACT_HISTORY_LIMIT:])

        try:
            async with channel.typing():
                reply = await self.ai.complete(messages)
        except Exception:  # pragma: no cover - defensive
            self.bot.logger.exception(
                "%s reply failed for %s", self._agent_name(contract), contract["case_id"]
            )
            reply = None

        if not reply:
            if faction == faction_rules.CROWN:
                reply = (
                    "The Court's ledger line has gone quiet for a moment — that is on "
                    "our side, not yours, and I apologise for the delay. An officer "
                    "needs to check the Court's link before I can put figures to paper. "
                    "Please put it to me again shortly."
                )
            else:
                reply = (
                    "Right then — my line to the Union's records has gone down, and "
                    "that is my problem to fix, not yours. An administrator needs to "
                    "check the AI link before I can quote, so please put it to me again "
                    "shortly."
                )

        history.append({"role": "assistant", "content": reply})
        await self.db.update_contract(
            contract["id"], history=json.dumps(history[-config.CONTRACT_HISTORY_LIMIT:])
        )
        await self._send_chunks(channel, reply)

    async def _agent_intro(self, contract, channel) -> None:
        owner_label = "kingdom" if self._owner_type(contract) == "kingdom" else "nation"
        agent = self._agent_name(contract)
        if self._contract_faction(contract) == faction_rules.CROWN:
            ask = (
                "ask for the goods offered, quantities, packaging, collection point "
                "and timing, and the asking price before any quote"
            )
        else:
            ask = (
                "ask for the material itemisation, coordinates and public-benefit "
                "justification before any quote"
            )
        await self._agent_reply(
            contract,
            channel,
            user_text=(
                f"Open the negotiation for {contract['case_id']}. The {owner_label} of "
                f"{contract['nation_name']} has proposed: {contract['project']}. "
                f"Budget requested: {contract['budget']:,} credits. Address the "
                f"requester directly as {agent}, thank them for bringing it forward, "
                f"state the Case ID, classify the funding tier, and {ask}."
            ),
        )

    # ------------------------------------------------------------------
    # Conversation listener
    # ------------------------------------------------------------------
    def _is_contract_space(self, channel) -> bool:
        """True for a cached contract thread, or one named *-contract-*."""
        if channel.id in self._thread_ids:
            return True
        name = getattr(channel, "name", "") or ""
        return name.startswith(("union-contract-", "crown-contract-")) and isinstance(
            channel, (discord.Thread, discord.TextChannel)
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return
        if not message.content.strip():
            return
        if not self._is_contract_space(message.channel):
            return

        contract = await self.db.get_contract_by_thread(message.channel.id)
        if contract is None or contract["status"] not in ("negotiating",):
            return

        # Cache the channel so subsequent messages skip the name check/DB probe.
        self._thread_ids.add(message.channel.id)

        settings = await self.db.get_settings(message.guild.id)
        if not self._can_speak(contract, message.author, settings):
            return

        lock = self._locks.setdefault(message.channel.id, asyncio.Lock())
        async with lock:
            await self._agent_reply(
                contract,
                message.channel,
                user_text=f"{message.author.display_name}: {message.content}",
            )

    # ------------------------------------------------------------------
    # Contract request, reached through /faction contract (either faction)
    # ------------------------------------------------------------------
    async def request_contract(
        self,
        interaction: discord.Interaction,
        account: str,
        proposal: str,
        budget: int,
        faction: str = "union",
    ) -> Optional[bool]:
        """Open a negotiation with the faction's representative.

        Membership is never checked: any nation or kingdom may request a
        contract from either faction. Returns True only when the case opened.
        """
        faction = (faction or "union").strip().lower()
        if faction not in (faction_rules.UNION, faction_rules.CROWN):
            return await interaction.response.send_message(
                embed=embeds.error("Unknown Faction", "Choose the Workers' Union or The Crown."),
                ephemeral=True,
            )
        agent = "Albert" if faction == faction_rules.CROWN else "Gilbert"
        faction_name = "Crown" if faction == faction_rules.CROWN else "Union"
        if interaction.guild is None:
            return await interaction.response.send_message(
                embed=embeds.error("Guild Only", "Contracts only work inside a server."), ephemeral=True
            )
        await interaction.response.defer(ephemeral=True, thinking=True)

        settings = await self.db.get_settings(interaction.guild_id)
        if not settings["contract_channel_id"]:
            return await interaction.followup.send(
                embed=embeds.error(
                    "Not Configured",
                    "An admin must run `/contract setchannel` before negotiations can open.",
                ),
                ephemeral=True,
            )

        owner_type, owner_row = await self._resolve_owner(interaction.guild_id, account)
        if owner_row is None:
            return await interaction.followup.send(
                embed=embeds.error("Not Found", f"No nation or kingdom named **{account}**."),
                ephemeral=True,
            )

        if not (config.CONTRACT_MIN_CREDITS <= budget <= config.CONTRACT_MAX_CREDITS):
            if config.CONTRACT_MIN_CREDITS == config.CONTRACT_MAX_CREDITS:
                detail = f"Budget must be exactly **{money(config.CONTRACT_MIN_CREDITS)}**."
            else:
                detail = (
                    f"Budget must be between **{money(config.CONTRACT_MIN_CREDITS)}** and "
                    f"**{money(config.CONTRACT_MAX_CREDITS)}**."
                )
            return await interaction.followup.send(
                embed=embeds.error("Invalid Budget", detail),
                ephemeral=True,
            )

        # A party can be carried beyond its own treasury, but only on the
        # leverage ladder: 3x funds up to 10M, 2x up to 20M, then 1x.
        if not budget_allowed(budget, owner_row["balance"]):
            return await interaction.followup.send(
                embed=embeds.error(
                    "Budget Exceeds Limit",
                    f"{owner_type.capitalize()} **{owner_row['name']}** holds "
                    f"**{money(owner_row['balance'])}**; "
                    + limit_explanation(
                        owner_row["balance"], balance_text="at that balance"
                    )
                    + f" A contract for **{money(budget)}** cannot be opened.",
                ),
                ephemeral=True,
            )

        if not (owner_row["leader_id"] == interaction.user.id or self._is_union_admin(interaction.user, settings)):
            return await interaction.followup.send(
                embed=embeds.error(
                    "Not Authorized",
                    f"Only the {owner_type}'s leader or a Union Admin may open a contract for it.",
                ),
                ephemeral=True,
            )

        cycle = await self.db.current_contract_cycle(interaction.guild_id)
        limit = config.CONTRACTS_PER_CYCLE.get(owner_type, 1)
        existing = await self.db.count_contracts_for_cycle(
            interaction.guild_id, owner_type, owner_row["id"], cycle, faction=faction
        )
        if existing >= limit:
            return await interaction.followup.send(
                embed=embeds.error(
                    "Cycle Limit Reached",
                    f"**{owner_row['name']}** already holds **{existing}** {faction_name} contract(s) "
                    f"in natural payout cycle **{cycle}**. A {owner_type} may hold at most "
                    f"**{limit}** per natural cycle — no exceptions.",
                ),
                ephemeral=True,
            )

        tag = _nation_tag(owner_row["name"])
        prefix = "CR" if faction == faction_rules.CROWN else "UN"
        number = await self.db.next_case_number(interaction.guild_id, tag, prefix=prefix)
        case_id = f"{prefix}-{tag}-{number:03d}"

        try:
            space = await self._create_negotiation_space(
                interaction, owner_row, case_id, settings, faction=faction
            )
        except Exception as exc:
            self.bot.logger.exception("Failed to create contract space: %s", exc)
            return await interaction.followup.send(
                embed=embeds.error(
                    "Ticket Failed",
                    "I couldn't open the negotiation ticket. Check my channel/thread permissions.",
                ),
                ephemeral=True,
            )

        contract_id = await self.db.create_contract(
            guild_id=interaction.guild_id,
            case_id=case_id,
            nation_id=owner_row["id"] if owner_type == "nation" else 0,
            nation_name=owner_row["name"],
            nation_tag=tag,
            requester_id=interaction.user.id,
            cycle=cycle,
            budget=budget,
            proposal=proposal,
            owner_type=owner_type,
            owner_id=owner_row["id"],
            quota_limit=limit,
            faction=faction,
        )
        if not contract_id:
            # The pre-check passed, but another request took the last slot
            # between that read and this insert. The insert's transaction is the
            # authority; remove the thread we opened for the losing request.
            try:
                await space.delete()
            except discord.HTTPException:
                self.bot.logger.warning(
                    "Could not delete thread %s after a lost contract-quota race",
                    space.id,
                )
            return await interaction.followup.send(
                embed=embeds.error(
                    "Cycle Limit Reached",
                    f"**{owner_row['name']}** has used all **{limit}** contract slot(s) "
                    f"for natural payout cycle **{cycle}**. Another request filled the "
                    "last slot just now; no case was opened.",
                ),
                ephemeral=True,
            )
        await self.db.set_contract_thread(contract_id, space.id)
        contract = await self.db.get_contract(contract_id)
        self._thread_ids.add(space.id)

        await space.send(
            embed=self._contract_embed(contract),
            view=ContractCloseView(self, contract_id),
        )
        await interaction.followup.send(
            embed=embeds.success(
                "Case Opened",
                f"Case **{case_id}** is open in {space.mention}. {agent} will be with you shortly.",
            ),
            ephemeral=True,
        )
        await self._agent_intro(contract, space)
        return True

    # ------------------------------------------------------------------
    # /contract close
    # ------------------------------------------------------------------
    @group.command(name="close", description="Close and archive a contract negotiation.")
    @app_commands.describe(case_id="Case ID (defaults to this thread's contract).")
    async def close(self, interaction: discord.Interaction, case_id: Optional[str] = None):
        if case_id:
            contract = await self.db.get_contract_by_case(case_id)
        else:
            contract = await self.db.get_contract_by_thread(interaction.channel_id)
        if contract is None:
            return await interaction.response.send_message(
                embed=embeds.error("Not Found", "No contract matches that case ID or thread."),
                ephemeral=True,
            )
        await self.close_contract(interaction, contract["id"])

    async def close_contract(self, interaction: discord.Interaction, contract_id: int) -> None:
        contract = await self.db.get_contract(contract_id)
        if contract is None:
            return await interaction.response.send_message(
                embed=embeds.error("Not Found", "That contract no longer exists."), ephemeral=True
            )
        if not self._same_guild(interaction, contract):
            return await self._wrong_guild(interaction)

        settings = await self.db.get_settings(contract["guild_id"])
        if not self._can_speak(contract, interaction.user, settings):
            return await interaction.response.send_message(
                embed=embeds.error("Not Authorized", "Only the requester or a Union Admin may close this ticket."),
                ephemeral=True,
            )

        if contract["status"] == "closed":
            return await interaction.response.send_message(
                embed=embeds.info("Already Closed", "This ticket is already closed."), ephemeral=True
            )
        if contract["status"] in ("rejected", "completed", "cancelled"):
            return await interaction.response.send_message(
                embed=embeds.error(
                    "Already Finished",
                    "That contract is already finished; close only applies to an active negotiation.",
                ),
                ephemeral=True,
            )

        now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        async with self.db.transaction():
            won = await self.db.transition_contract(
                contract["id"],
                from_statuses=("negotiating", "approved"),
                to_status="closed",
                closed_at=now,
            )
        if not won:
            fresh = await self.db.get_contract(contract["id"])
            return await interaction.response.send_message(
                embed=embeds.info(
                    "Already Closed",
                    f"That contract is now **{fresh['status'] if fresh else 'gone'}**.",
                ),
                ephemeral=True,
            )
        if contract["thread_id"]:
            self._thread_ids.discard(int(contract["thread_id"]))

        await interaction.response.send_message(
            embed=embeds.success("Ticket Closed", f"**{contract['case_id']}** has been closed and archived."),
            ephemeral=True,
        )

        channel = await self._get_channel(interaction.guild, contract["thread_id"])
        if channel is not None:
            try:
                if isinstance(channel, discord.Thread):
                    await channel.edit(archived=True, locked=True, reason=f"{self._contract_label(contract)} contract closed")
                elif isinstance(channel, discord.TextChannel):
                    await channel.set_permissions(
                        interaction.guild.default_role, send_messages=False, reason=f"{self._contract_label(contract)} contract closed"
                    )
            except discord.HTTPException:
                pass

    # ------------------------------------------------------------------
    # /contract status & list
    # ------------------------------------------------------------------
    @group.command(name="status", description="Show a contract's details and tranches.")
    @app_commands.describe(case_id="Case ID (defaults to this thread's contract).")
    async def status(self, interaction: discord.Interaction, case_id: Optional[str] = None):
        if case_id:
            contract = await self.db.get_contract_by_case(case_id)
        else:
            contract = await self.db.get_contract_by_thread(interaction.channel_id)
        if contract is None:
            return await interaction.response.send_message(
                embed=embeds.error("Not Found", "No contract matches that case ID or thread."),
                ephemeral=True,
            )
        if not self._same_guild(interaction, contract):
            return await self._wrong_guild(interaction)

        settings = await self.db.get_settings(contract["guild_id"])
        if not (self._can_speak(contract, interaction.user, settings)):
            return await interaction.response.send_message(
                embed=embeds.error("Not Authorized", "You cannot view that contract."), ephemeral=True
            )

        _, claim_row = await self._claim_target(contract)
        await interaction.response.send_message(
            embed=self._contract_embed(
                contract, detailed=True, claim_name=claim_row["name"] if claim_row else None
            ),
            ephemeral=True,
        )

    @group.command(name="list", description="List faction contracts.")
    async def list_contracts(self, interaction: discord.Interaction):
        await self._send_contract_list(interaction)

    async def _send_contract_list(
        self,
        interaction: discord.Interaction,
        *,
        faction: Optional[str] = None,
        account: Optional[str] = None,
    ) -> None:
        """Render the contract list, optionally scoped to one faction/account."""
        if interaction.guild_id is None:
            return await interaction.response.send_message(
                embed=embeds.error("Guild Only", "Contracts only work inside a server."),
                ephemeral=True,
            )
        owner_type = owner_id = None
        if account:
            owner_type, row = await self._resolve_owner(interaction.guild_id, account)
            if row is None:
                return await interaction.response.send_message(
                    embed=embeds.error("Not Found", f"No nation or kingdom named **{account}**."),
                    ephemeral=True,
                )
            owner_id = int(row["id"])
        settings = await self.db.get_settings(interaction.guild_id)
        staff = self._is_union_admin(interaction.user, settings)
        rows = await self.db.list_contracts(
            interaction.guild_id,
            requester_id=None if staff else interaction.user.id,
            faction=faction,
            owner_type=owner_type,
            owner_id=owner_id,
        )
        if not rows:
            return await interaction.response.send_message(
                embed=embeds.info("No Contracts", "No contracts to show."), ephemeral=True
            )
        lines = []
        for r in rows[:20]:
            icon = "👑" if self._owner_type(r) == "kingdom" else "🏛️"
            badge = "👑" if self._contract_faction(r) == faction_rules.CROWN else "⚒️"
            lines.append(
                f"{badge} `{r['case_id']}` · {icon} **{r['nation_name']}** · "
                f"{money(r['budget'])} · {STATUS_LABELS.get(r['status'], r['status'])}"
            )
        if faction == faction_rules.CROWN:
            title, footer = "👑 Crown Contracts", "⚜ The Realm of Rune · The Crown"
        elif faction == faction_rules.UNION:
            title, footer = "🛠️ Union Contracts", "⚜ The Realm of Rune · Workers' Union"
        else:
            title, footer = "🛠️ Faction Contracts", "⚜ The Realm of Rune · Royal Court"
        embed = discord.Embed(
            title=title,
            description="\n".join(lines),
            color=config.GOLD_COLOR,
        )
        embed.set_footer(text=footer)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def list_for_faction(
        self,
        interaction: discord.Interaction,
        faction: str,
        account: Optional[str] = None,
    ) -> None:
        """`/faction contracts` entry point: one faction, optional account."""
        await self._send_contract_list(interaction, faction=faction, account=account)

    async def _approve_checked(self, contract_id: int, claim_account: Optional[str]):
        """Re-read, validate and approve one contract inside a transaction.

        Everything that gates approval — status, budget, owner balance, the
        funding faction's treasury and claim account — is read under the same
        lock as the guarded status flip. A concurrent transfer or withdrawal
        therefore cannot slip between the check and the approval.
        """
        async with self.db.transaction():
            fresh = await self.db.get_contract(contract_id)
            if fresh is None:
                raise ApprovalError("Not Found", "That contract no longer exists.")
            if fresh["status"] != "negotiating":
                raise ApprovalError(
                    "Wrong Status",
                    f"That contract is **{fresh['status']}**, not negotiating.",
                )
            if fresh["approved_at"] is not None:
                raise ApprovalError(
                    "Already Funded",
                    "This contract was already funded once. A reopened case is discussion-only; "
                    "open a new case for a new budget.",
                )
            if not (
                config.CONTRACT_MIN_CREDITS
                <= fresh["budget"]
                <= config.CONTRACT_MAX_CREDITS
            ):
                if config.CONTRACT_MIN_CREDITS == config.CONTRACT_MAX_CREDITS:
                    detail = (
                        "That budget is not the required "
                        f"**{money(config.CONTRACT_MIN_CREDITS)}**."
                    )
                else:
                    detail = (
                        f"That budget is outside the allowed range "
                        f"**{money(config.CONTRACT_MIN_CREDITS)}**–"
                        f"**{money(config.CONTRACT_MAX_CREDITS)}**."
                    )
                raise ApprovalError("Budget Out Of Range", detail)

            owner_type = self._owner_type(fresh)
            owner = await self._owner_row(fresh)
            if owner is None:
                raise ApprovalError("Not Found", f"That {owner_type} no longer exists.")
            if not budget_allowed(fresh["budget"], owner["balance"]):
                raise ApprovalError(
                    "Budget Exceeds Limit",
                    f"**{owner['name']}** holds **{money(owner['balance'])}**. "
                    + limit_explanation(owner["balance"], balance_text="At that balance")
                    + f" A **{money(fresh['budget'])}** contract cannot be approved.",
                )

            # The funding faction pays, so its treasury must actually hold the
            # money. This is the only cap on money creation; approve never mints
            # credits. Crown legacy supplier contracts reserve part of its
            # balance, so only the unreserved remainder can fund a new case.
            faction = self._contract_faction(fresh)
            if faction == faction_rules.CROWN:
                crown_balance = await self.db.get_crown_balance(fresh["guild_id"])
                reserved = await self.db.crown_reserved(fresh["guild_id"])
                available = max(0, crown_balance - reserved)
                if available < fresh["budget"]:
                    raise ApprovalError(
                        "Crown Treasury Short",
                        f"The Crown holds **{money(crown_balance)}** and has already "
                        f"reserved **{money(reserved)}** for legacy supplier contracts, "
                        f"leaving **{money(available)}** for **{fresh['case_id']}**, which "
                        f"needs **{money(fresh['budget'])}** in total.\nAn admin must add "
                        f"at least **{money(fresh['budget'] - available)}** with "
                        "`/faction fund faction:crown` before this contract can be approved.",
                    )
            else:
                union_balance = await self.db.get_union_balance(fresh["guild_id"])
                if union_balance < fresh["budget"]:
                    raise ApprovalError(
                        "Union Treasury Short",
                        f"The Workers' Union holds **{money(union_balance)}** but "
                        f"**{fresh['case_id']}** needs **{money(fresh['budget'])}** in total.\n"
                        f"An admin must add at least "
                        f"**{money(fresh['budget'] - union_balance)}** with "
                        "`/faction fund faction:union` before this contract can be approved.",
                    )

            if claim_account:
                claim_type, claim_row = await self._resolve_owner(
                    fresh["guild_id"], claim_account
                )
            else:
                claim_type, claim_row = owner_type, owner
            if claim_row is None:
                raise ApprovalError(
                    "Not Found", f"No nation or kingdom named **{claim_account}**."
                )

            upfront = fresh["budget"] * config.CONTRACT_UPFRONT_PCT // 100
            escrow = fresh["budget"] - upfront
            now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
            won = await self.db.approve_contract(
                fresh["id"],
                upfront=upfront,
                escrow=escrow,
                approved_at=now,
                claim_account_type=claim_type,
                claim_account_id=claim_row["id"],
            )
            if not won:
                raise ApprovalError(
                    "Already Funded",
                    "This contract was approved by someone else just now.",
                )
            updated = await self.db.get_contract(fresh["id"])
        assert updated is not None
        return updated, claim_type, claim_row

    # ------------------------------------------------------------------
    # /contract approve · release · reject
    # ------------------------------------------------------------------
    @group.command(name="approve", description="[Union Admin] Approve a contract — the funding faction pays the 25% upfront tranche.")
    @app_commands.describe(
        case_id="The contract case ID.",
        claim_account="Account that receives the credits (defaults to the contracting party).",
    )
    @app_commands.autocomplete(claim_account=account_autocomplete)
    async def approve(
        self,
        interaction: discord.Interaction,
        case_id: str,
        claim_account: Optional[str] = None,
    ):
        if interaction.guild_id is None:
            return await interaction.response.send_message(
                embed=embeds.error("Guild Only", "Contracts only work inside a server."),
                ephemeral=True,
            )
        await interaction.response.defer(ephemeral=True, thinking=True)
        settings = await self.db.get_settings(interaction.guild_id)
        if not self._is_union_admin(interaction.user, settings):
            return await interaction.followup.send(
                embed=embeds.error("Not Authorized", "Only Union Admins may approve contracts."),
                ephemeral=True,
            )

        contract = await self.db.get_contract_by_case(case_id)
        if contract is None:
            return await interaction.followup.send(
                embed=embeds.error("Not Found", "No contract matches that case ID."), ephemeral=True
            )
        if not self._same_guild(interaction, contract):
            return await self._wrong_guild(interaction)
        try:
            contract, claim_type, claim_row = await self._approve_checked(
                contract["id"], claim_account
            )
        except ApprovalError as exc:
            return await interaction.followup.send(
                embed=embeds.error(exc.title, exc.detail), ephemeral=True
            )
        upfront = int(contract["upfront"])
        escrow = int(contract["escrow"])

        channel = await self._get_channel(interaction.guild, contract["thread_id"])
        if channel is not None:
            try:
                await channel.send(
                    embed=self._contract_embed(contract, detailed=True, claim_name=claim_row["name"])
                )
            except discord.HTTPException:
                pass

        await self._post_to_board(contract, claim_row["name"])

        funder = (
            "The Crown" if self._contract_label(contract) == "Crown"
            else "the Workers' Union"
        )
        dm_ok = await self._dm_contractor(
            interaction.guild,
            contract,
            title="Contract Approved",
            body=(
                f"Case **{contract['case_id']}** is approved and funded by {funder}.\n\n"
                f"**{money(upfront)}** (25%) is claimable now for materials and building.\n"
                f"**{money(escrow)}** (75%) unlocks after final inspection, for upkeep.\n\n"
                f"Claim with `/contract claim {contract['case_id']}` or the Claim Credits button "
                f"on the contract board. Claimed credits land in "
                f"**{claim_row['name']}**, ready to pay your builders."
            ),
        )

        await interaction.followup.send(
            embed=embeds.success(
                "Contract Approved",
                f"**{contract['case_id']}** approved and funded from the "
                f"{self._contract_label(contract)} treasury. "
                f"**{money(upfront)}** is claimable now by "
                f"{account_emoji(claim_type)} **{claim_row['name']}**; "
                f"**{money(escrow)}** unlocks on release."
                + ("" if dm_ok else " ⚠️ I couldn't DM the contractor."),
            ),
            ephemeral=True,
        )

    @group.command(name="release", description="[Union Admin] Final inspection passed — release the escrow tranche.")
    @app_commands.describe(case_id="The contract case ID.")
    async def release(self, interaction: discord.Interaction, case_id: str):
        if interaction.guild_id is None:
            return await interaction.response.send_message(
                embed=embeds.error("Guild Only", "Contracts only work inside a server."),
                ephemeral=True,
            )
        await interaction.response.defer(ephemeral=True, thinking=True)
        settings = await self.db.get_settings(interaction.guild_id)
        if not self._is_union_admin(interaction.user, settings):
            return await interaction.followup.send(
                embed=embeds.error("Not Authorized", "Only Union Admins may release escrow."),
                ephemeral=True,
            )

        contract = await self.db.get_contract_by_case(case_id)
        if contract is None:
            return await interaction.followup.send(
                embed=embeds.error("Not Found", "No contract matches that case ID."), ephemeral=True
            )
        if not self._same_guild(interaction, contract):
            return await self._wrong_guild(interaction)
        if contract["approved_at"] is None or contract["status"] not in (
            "approved",
            "negotiating",
            "closed",
        ):
            return await interaction.followup.send(
                embed=embeds.error(
                    "Wrong Status",
                    f"That contract is **{contract['status']}** and has not been funded.",
                ),
                ephemeral=True,
            )

        now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        # The status flip and its ledger row land together, like every other
        # state change the ledger is expected to explain. The status guard stops
        # a release retried after a concurrent reject from paying the escrow.
        async with self.db.transaction():
            won = await self.db.transition_contract(
                contract["id"],
                from_statuses=("approved", "negotiating", "closed"),
                to_status="completed",
                released=contract["escrow"],
                completed_at=now,
            )
            if won:
                await self.db.log_tx(
                    guild_id=contract["guild_id"],
                    kind="contract_release",
                    account_type=self._owner_type(contract),
                    account_id=self._owner_id(contract),
                    account_name=contract["nation_name"],
                    amount=0,
                    actor_id=interaction.user.id,
                    note=f"{contract['case_id']} escrow {contract['escrow']} unlocked for claim",
                )
        if not won:
            fresh = await self.db.get_contract(contract["id"])
            return await interaction.followup.send(
                embed=embeds.error(
                    "Already Changed",
                    f"That contract is now **{fresh['status'] if fresh else 'gone'}**; the escrow was not released.",
                ),
                ephemeral=True,
            )

        contract = await self.db.get_contract(contract["id"])
        claim_type, claim_row = await self._claim_target(contract)
        dm_ok = await self._dm_contractor(
            interaction.guild,
            contract,
            title="Escrow Released",
            body=(
                f"Final inspection passed for **{contract['case_id']}**. The remaining "
                f"**{money(contract['escrow'])}** is now claimable.\n\n"
                f"Claim with `/contract claim {contract['case_id']}` or the Claim Credits "
                f"button on the contract board."
            ),
        )

        await self._refresh_board(contract, claim_row["name"] if claim_row else None)

        await interaction.followup.send(
            embed=embeds.success(
                "Escrow Released",
                f"**{money(contract['escrow'])}** is now claimable by "
                + (
                    f"{account_emoji(claim_type)} **{claim_row['name']}**."
                    if claim_row
                    else "the contractor."
                )
                + ("" if dm_ok else " ⚠️ I couldn't DM the contractor."),
            ),
            ephemeral=True,
        )

    @group.command(name="reject", description="[Union Admin] Reject a contract and stop further payments.")
    @app_commands.describe(case_id="The contract case ID.", reason="Why it was rejected.")
    async def reject(self, interaction: discord.Interaction, case_id: str, reason: Optional[str] = None):
        if interaction.guild_id is None:
            return await interaction.response.send_message(
                embed=embeds.error("Guild Only", "Contracts only work inside a server."),
                ephemeral=True,
            )
        await interaction.response.defer(ephemeral=True, thinking=True)
        settings = await self.db.get_settings(interaction.guild_id)
        if not self._is_union_admin(interaction.user, settings):
            return await interaction.followup.send(
                embed=embeds.error("Not Authorized", "Only Union Admins may reject contracts."),
                ephemeral=True,
            )

        contract = await self.db.get_contract_by_case(case_id)
        if contract is None:
            return await interaction.followup.send(
                embed=embeds.error("Not Found", "No contract matches that case ID."), ephemeral=True
            )
        if not self._same_guild(interaction, contract):
            return await self._wrong_guild(interaction)
        if contract["status"] in ("completed", "cancelled", "rejected"):
            return await interaction.followup.send(
                embed=embeds.error("Already Closed", "That contract is already finished."), ephemeral=True
            )

        now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        # No refund bookkeeping: the funding faction pays the party, so rejecting only
        # stops future payments. Tranches already claimed are kept; tranches
        # never claimed are forfeited (the claim command refuses rejected cases).
        async with self.db.transaction():
            won = await self.db.transition_contract(
                contract["id"],
                from_statuses=("negotiating", "approved", "closed"),
                to_status="rejected",
                closed_at=now,
            )
            if won and (contract["upfront_claimed"] or contract["final_claimed"]):
                await self.db.log_tx(
                    guild_id=contract["guild_id"],
                    kind="contract_reject",
                    account_type=self._owner_type(contract),
                    account_id=self._owner_id(contract),
                    account_name=contract["nation_name"],
                    amount=0,
                    actor_id=interaction.user.id,
                    note=f"{contract['case_id']} rejected · unclaimed tranches forfeited",
                )
        if not won:
            fresh = await self.db.get_contract(contract["id"])
            return await interaction.followup.send(
                embed=embeds.error(
                    "Already Changed",
                    f"That contract is now **{fresh['status'] if fresh else 'gone'}**; it was not rejected.",
                ),
                ephemeral=True,
            )

        if contract["thread_id"]:
            self._thread_ids.discard(int(contract["thread_id"]))
        channel = await self._get_channel(interaction.guild, contract["thread_id"])
        if isinstance(channel, discord.Thread):
            try:
                await channel.edit(archived=True, locked=True, reason=f"{self._contract_label(contract)} contract rejected")
            except discord.HTTPException:
                pass

        await interaction.followup.send(
            embed=embeds.success(
                "Contract Rejected",
                f"**{contract['case_id']}** rejected. No further credits will be paid."
                + (f"\nReason: {reason}" if reason else ""),
            ),
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # Claiming credits from the funding faction
    # ------------------------------------------------------------------
    async def _claim_target(self, contract):
        """(account_type, row) that this contract's credits are paid into."""
        account_type = contract["claim_account_type"] or self._owner_type(contract)
        account_id = contract["claim_account_id"]
        if account_id is None:
            account_id = self._owner_id(contract)
        if account_type == "kingdom":
            row = await self.db.get_kingdom_by_id(int(account_id))
        else:
            row = await self.db.get_nation_by_id(int(account_id))
        if row is None or int(row["guild_id"]) != int(contract["guild_id"]):
            return account_type, None
        return account_type, row

    @staticmethod
    def _legacy_funded(contract) -> bool:
        """Cases approved before the Union paid contractors.

        Those parties were charged the full budget at approval and given in-game
        keys. The whole budget is therefore owed back to them; new cases are
        never charged, so nothing is owed beyond the granted tranches.
        """
        return contract["approved_at"] is not None and bool(contract["redeem_key"])

    def _claimable_tranches(self, contract) -> list[tuple[str, int]]:
        """Tranches payable right now, ignoring whether they were claimed yet."""
        if contract["approved_at"] is None:
            return []
        legacy = self._legacy_funded(contract)
        if contract["status"] in ("rejected", "cancelled") and not legacy:
            return []
        parts: list[tuple[str, int]] = []
        if (
            not contract["upfront_claimed"]
            and not contract["upfront_redeemed"]
            and int(contract["upfront"]) > 0
        ):
            parts.append(("upfront", int(contract["upfront"])))
        if (
            (legacy or contract["released"] > 0)
            and not contract["final_claimed"]
            and not contract["final_redeemed"]
            and int(contract["escrow"]) > 0
        ):
            parts.append(("final", int(contract["escrow"])))
        return parts

    async def _pay_claim(self, contract, actor) -> tuple[int, list[str]]:
        """Pay every eligible, unclaimed tranche into the claim account.

        Credits come out of the funding faction's treasury. The flag flip, the
        treasury debit, the recipient credit and both ledger rows share one
        transaction, so a retried button, a race, or a short treasury cannot pay
        twice or pay money that does not exist.
        """
        paid = 0
        lines: list[str] = []
        async with self.db.transaction():
            # Re-read under the transaction lock: a reject or close that
            # committed while the command was being handled must not be paid
            # out of a stale snapshot.
            fresh = await self.db.get_contract(contract["id"])
            if fresh is None:
                raise ClaimError("That contract no longer exists.")
            if fresh["status"] in ("rejected", "cancelled") and not self._legacy_funded(fresh):
                raise ClaimError("That contract was rejected; its unclaimed tranches are forfeited.")
            contract = fresh
            faction = self._contract_faction(contract)
            crown_funded = faction == faction_rules.CROWN
            funder_label = "The Crown" if crown_funded else "the Workers' Union"
            funder_type = "crown" if crown_funded else "union"
            fund_cmd = (
                "/faction fund faction:crown" if crown_funded
                else "/faction fund faction:union"
            )
            account_type, target = await self._claim_target(contract)
            if target is None:
                raise ClaimError("The account that should receive these credits no longer exists.")
            table = "kingdoms" if account_type == "kingdom" else "nations"

            for kind, amount in self._claimable_tranches(contract):
                if not await self.db.claim_contract_tranche(contract["id"], kind):
                    continue
                try:
                    if crown_funded:
                        funder_after = await self.db.debit_crown(contract["guild_id"], amount)
                    else:
                        funder_after = await self.db.debit_union(contract["guild_id"], amount)
                except InsufficientFunds as exc:
                    # Rolls back the tranche flag too, so the claim can be
                    # retried once the treasury is funded.
                    raise ClaimError(
                        f"{funder_label} holds **{money(exc.balance)}** but this "
                        f"tranche needs **{money(amount)}**. An admin must top up "
                        f"the treasury with `{fund_cmd}`."
                    ) from exc
                after = await self.db.add_balance(table, target["id"], amount)
                await self.db.log_tx(
                    guild_id=contract["guild_id"],
                    kind="contract_grant",
                    account_type=account_type,
                    account_id=target["id"],
                    account_name=target["name"],
                    counterparty_type=funder_type,
                    counterparty_name="The Crown" if crown_funded else "Workers' Union",
                    amount=amount,
                    balance_after=after,
                    actor_id=actor.id,
                    note=(
                        f"{contract['case_id']} "
                        f"{'upfront 25%' if kind == 'upfront' else 'completion 75%'} "
                        f"paid by {funder_label}"
                    ),
                )
                await self.db.log_tx(
                    guild_id=contract["guild_id"],
                    kind="contract_grant",
                    account_type=funder_type,
                    account_id=contract["guild_id"],
                    account_name="The Crown" if crown_funded else "Workers' Union",
                    counterparty_type=account_type,
                    counterparty_id=target["id"],
                    counterparty_name=target["name"],
                    amount=-amount,
                    balance_after=funder_after,
                    actor_id=actor.id,
                    note=f"{contract['case_id']} {'upfront' if kind == 'upfront' else 'completion'} tranche",
                )
                paid += amount
                lines.append(
                    f"{'Upfront (25%)' if kind == 'upfront' else 'Completion (75%)'}: **{money(amount)}**"
                )
            committed = 0
            if paid:
                committed = await self.db.commit_balance_to_union(
                    contract["guild_id"], account_type, target["id"]
                )
                if committed:
                    lines.append(f"Committed to the Union: **{money(committed)}**")
            if paid:
                await self.db.update_contract(
                    contract["id"],
                    claimed_by=actor.id,
                    claimed_at=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
                )
        return paid, lines

    async def _handle_claim(self, interaction: discord.Interaction, contract) -> None:
        if not self._same_guild(interaction, contract):
            return await self._wrong_guild(interaction)
        await interaction.response.defer(ephemeral=True, thinking=True)
        settings = await self.db.get_settings(contract["guild_id"])
        account_type, target = await self._claim_target(contract)
        allowed = (
            self._is_union_admin(interaction.user, settings)
            or interaction.user.id == contract["requester_id"]
            or (target is not None and target["leader_id"] == interaction.user.id)
        )
        if not allowed:
            return await interaction.followup.send(
                embed=embeds.error(
                    "Not Authorized",
                    "Only the contracting party's leader, the requester, or a Union Admin may claim.",
                ),
                ephemeral=True,
            )

        try:
            paid, lines = await self._pay_claim(contract, interaction.user)
        except ClaimError as exc:
            return await interaction.followup.send(embed=embeds.error("Cannot Claim", str(exc)), ephemeral=True)

        contract = await self.db.get_contract(contract["id"])
        claim_name = target["name"] if target else None
        await self._refresh_board(contract, claim_name)

        if not paid:
            return await interaction.followup.send(
                embed=embeds.info(
                    "Nothing to Claim",
                    f"**{contract['case_id']}** has no tranche payable right now.\n"
                    f"The 75% completion tranche unlocks when "
                    f"{self._contract_label(contract)} releases escrow.",
                ),
                ephemeral=True,
            )

        return await interaction.followup.send(
            embed=embeds.success(
                "Credits Claimed",
                f"{chr(10).join(lines)}\n\nPaid into {account_emoji(account_type)} "
                f"**{claim_name}**. Transfer it to your builders with `/bank transfer`.",
            ),
            ephemeral=True,
        )

    async def claim_from_board(self, interaction: discord.Interaction, contract_id: int) -> None:
        contract = await self.db.get_contract(int(contract_id))
        if contract is None:
            return await interaction.response.send_message(
                embed=embeds.error("Not Found", "That contract no longer exists."), ephemeral=True
            )
        await self._handle_claim(interaction, contract)

    @group.command(name="claim", description="Claim the credits the contracting faction owes on a contract.")
    @app_commands.describe(case_id="The contract case ID.")
    async def claim(self, interaction: discord.Interaction, case_id: str):
        contract = await self.db.get_contract_by_case(case_id)
        if contract is None:
            return await interaction.response.send_message(
                embed=embeds.error("Not Found", "No contract matches that case ID."), ephemeral=True
            )
        await self._handle_claim(interaction, contract)

    # ------------------------------------------------------------------
    # Contract board
    # ------------------------------------------------------------------
    async def _post_to_board(self, contract, claim_name: Optional[str]) -> bool:
        settings = await self.db.get_settings(contract["guild_id"])
        channel_id = settings["contract_board_channel_id"]
        if not channel_id:
            return False
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except Exception:
                return False
        try:
            message = await channel.send(
                embed=self._contract_embed(contract, detailed=True, claim_name=claim_name),
                view=ContractClaimView(self, contract["id"]),
            )
        except discord.HTTPException:
            return False
        self.bot.add_view(ContractClaimView(self, contract["id"]))
        await self.db.update_contract(
            contract["id"], board_channel_id=channel.id, board_message_id=message.id
        )
        return True

    async def _refresh_board(self, contract, claim_name: Optional[str]) -> None:
        if not contract["board_message_id"] or not contract["board_channel_id"]:
            return
        channel = self.bot.get_channel(contract["board_channel_id"])
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(contract["board_channel_id"])
            except Exception:
                return
        try:
            message = await channel.fetch_message(contract["board_message_id"])
            await message.edit(
                embed=self._contract_embed(contract, detailed=True, claim_name=claim_name)
            )
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

    @group.command(
        name="reopen",
        description="[Union Admin] Reopen an approved, closed or completed contract.",
    )
    @app_commands.describe(case_id="The contract case ID.")
    async def reopen(self, interaction: discord.Interaction, case_id: str):
        if interaction.guild_id is None:
            return await interaction.response.send_message(
                embed=embeds.error("Guild Only", "Contracts only work inside a server."),
                ephemeral=True,
            )
        await interaction.response.defer(ephemeral=True, thinking=True)
        settings = await self.db.get_settings(interaction.guild_id)
        if not self._is_union_admin(interaction.user, settings):
            return await interaction.followup.send(
                embed=embeds.error("Not Authorized", "Only Union Admins may reopen contracts."),
                ephemeral=True,
            )

        contract = await self.db.get_contract_by_case(case_id)
        if contract is None:
            return await interaction.followup.send(
                embed=embeds.error("Not Found", "No contract matches that case ID."), ephemeral=True
            )
        if not self._same_guild(interaction, contract):
            return await self._wrong_guild(interaction)
        if contract["status"] == "negotiating":
            return await interaction.followup.send(
                embed=embeds.info("Already Open", "That contract is already negotiating."), ephemeral=True
            )

        funded = contract["approved_at"] is not None
        async with self.db.transaction():
            won = await self.db.transition_contract(
                contract["id"],
                from_statuses=self.REOPENABLE_STATUSES,
                to_status="negotiating",
                closed_at=None,
            )
        if not won:
            fresh = await self.db.get_contract(contract["id"])
            return await interaction.followup.send(
                embed=embeds.error(
                    "Already Changed",
                    f"That contract is now **{fresh['status'] if fresh else 'gone'}**.",
                ),
                ephemeral=True,
            )
        contract = await self.db.get_contract(contract["id"])

        channel = await self._get_channel(interaction.guild, contract["thread_id"])
        restored = False
        if isinstance(channel, discord.Thread):
            try:
                kwargs = {}
                if channel.archived:
                    kwargs["archived"] = False
                if channel.locked:
                    kwargs["locked"] = False
                if kwargs:
                    await channel.edit(reason="Contract reopened", **kwargs)
                restored = True
            except discord.HTTPException:
                pass
        elif isinstance(channel, discord.TextChannel):
            try:
                await channel.set_permissions(
                    interaction.guild.default_role, send_messages=None, reason="Contract reopened"
                )
                restored = True
            except discord.HTTPException:
                pass

        if channel is not None:
            self._thread_ids.add(channel.id)
            try:
                note = (
                    f"🔓 Case **{contract['case_id']}** reopened by {interaction.user.mention}. "
                    "Gilbert has resumed the negotiation."
                )
                if funded:
                    note += (
                        " The funding already agreed stands — this reopened case is for "
                        "discussion and inspection only; a new budget needs a new case."
                    )
                await channel.send(note)
                history = json.loads(contract["history"] or "[]")
                history.append(
                    {
                        "role": "user",
                        "content": (
                            f"(Union Admin {interaction.user.display_name} reopened case "
                            f"{contract['case_id']}.)"
                        ),
                    }
                )
                await self.db.update_contract(
                    contract["id"], history=json.dumps(history[-config.CONTRACT_HISTORY_LIMIT:])
                )
            except discord.HTTPException:
                pass

        await interaction.followup.send(
            embed=embeds.success(
                "Contract Reopened",
                f"**{contract['case_id']}** is back in negotiation."
                + ("" if restored else " ⚠️ The original thread could not be restored.")
                + (
                    "\nFunding is preserved; re-approval is blocked to prevent a double debit."
                    if funded
                    else ""
                ),
            ),
            ephemeral=True,
        )

    async def _dm_contractor(self, guild: discord.Guild, contract, *, title: str, body: str) -> bool:
        member = await self._get_member(guild, contract["requester_id"])
        if member is None:
            return False
        try:
            await member.send(embed=embeds.success(title, body))
            return True
        except (discord.Forbidden, discord.HTTPException):
            return False

    # ------------------------------------------------------------------
    # /contract admin configuration
    # ------------------------------------------------------------------
    @group.command(name="setboard", description="[Admin] Set the channel that lists funded contracts with a Claim button.")
    @app_commands.describe(channel="Channel that will hold the contract board.")
    @admin_only()
    async def setboard(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await self.db.set_contract_board_channel(interaction.guild_id, channel.id)
        await interaction.response.send_message(
            embed=embeds.success(
                "Contract Board Set",
                f"Funded contracts will be posted in {channel.mention} with a Claim Credits button.",
            ),
            ephemeral=True,
        )

    @group.command(name="setchannel", description="[Admin] Set the channel where negotiation threads open.")
    @app_commands.describe(channel="The parent text channel.")
    @admin_only()
    async def setchannel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await self.db.set_contract_channel(interaction.guild_id, channel.id)
        await interaction.response.send_message(
            embed=embeds.success(
                "Channel Set",
                f"Negotiation tickets will open under {channel.mention}.",
            ),
            ephemeral=True,
        )

    @group.command(name="setrole", description="[Admin] Set the Union Admin role.")
    @app_commands.describe(role="The role that may manage contracts.")
    @admin_only()
    async def setrole(self, interaction: discord.Interaction, role: discord.Role):
        await self.db.set_union_admin_role(interaction.guild_id, role.id)
        await interaction.response.send_message(
            embed=embeds.success("Role Set", f"**{role.name}** is now the Union Admin role."),
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Contract(bot))
