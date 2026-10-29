"""
cogs/kits.py
============
Player-created kits (public item bundles).

Rules (from the spec):
  * One kit per player per 7 days.
  * Up to 10 distinct item types per kit, unlimited quantity each.
  * Kits are always delivered in a Shulker Box (mandatory fee).
  * New kits are published to the configured Public Kit Channel.
"""
from __future__ import annotations

import datetime as dt
import math

import discord
from discord import app_commands
from discord.ext import commands

import config
import embeds
from checks import admin_only
from database import parse_iso
from shop import pricing
from shop.checkout import ShopError, checkout_kit
from shop.embeds import order_embed
from utils import account_autocomplete, account_emoji, is_admin, money, resolve_account


def _parse_kit_line(line: str) -> tuple[str | None, int]:
    """Parse ``<item_key> [xQTY|QTY]`` into ``(token, quantity)``."""
    key: str | None = None
    qty = 1
    for token in line.strip().split():
        t = token.lower()
        if t.startswith("x") and t[1:].isdigit():
            qty = int(t[1:])
        elif t.isdigit():
            qty = int(t)
        elif key is None:
            key = token
    return key, qty


class KitItemsModal(discord.ui.Modal, title="Kit Contents"):
    items = discord.ui.TextInput(
        label="Items — one per line, max 10 types",
        style=discord.TextStyle.paragraph,
        placeholder="minecraft:oak_log x64\nminecraft:iron_ingot x128\nminecraft:bow x1",
        required=True,
        max_length=1000,
    )

    def __init__(self, cog: "Kits", name: str) -> None:
        super().__init__()
        self.cog = cog
        self.name = name

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog._on_kit_submit(interaction, self.name, self.items.value)


class Kits(commands.Cog):
    """📦 Player-created kits — public item bundles."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @property
    def db(self):
        return self.bot.db  # type: ignore[attr-defined]

    group = app_commands.Group(name="kit", description="📦 Player-created kits.")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    async def _catalog_map(self) -> dict[str, dict]:
        return {r["key"]: r for r in await self.db.list_catalog()}

    async def _resolve_item(self, catalog: list, token: str):
        token = token.strip()
        row = next((r for r in catalog if r["key"].lower() == token.lower()), None)
        if row:
            return row
        if ":" not in token:
            row = next((r for r in catalog if r["key"].lower() == f"minecraft:{token.lower()}"), None)
            if row:
                return row
        lowered = token.lower()
        matches = [r for r in catalog if lowered in r["key"].lower() or lowered in r["display_name"].lower()]
        return matches[0] if len(matches) == 1 else None

    def _kit_summary(self, kit_items: list, cmap: dict[str, dict]) -> tuple[int, list[str]]:
        items_total = 0
        lines: list[str] = []
        for it in kit_items:
            cat = cmap.get(it["item_key"])
            name = cat["display_name"] if cat else it["item_key"]
            qty = int(it["quantity"])
            unit_qty = (cat["unit_qty"] if cat else 1) or 1
            units = math.ceil(qty / unit_qty)
            price = cat["price"] if cat else 0
            items_total += units * price
            lines.append(f"`{qty:>5} ×` **{name}**")
        return items_total, lines

    def _kit_price(self, items_total: int) -> int:
        return items_total + config.SHULKER_BOX_FEE

    def _kit_embed(self, kit, kit_items: list, cmap: dict[str, dict], *, creator_name: str | None = None) -> discord.Embed:
        items_total, lines = self._kit_summary(kit_items, cmap)
        price = self._kit_price(items_total)
        creator = creator_name or f"<@{kit['creator_id']}>"
        embed = discord.Embed(title=f"📦 Kit #{kit['id']} · {kit['name']}", color=config.GOLD_COLOR)
        embed.add_field(name="Creator", value=creator, inline=True)
        embed.add_field(name="Price (before treasury discount)", value=f"**{money(price)}**", inline=True)
        embed.add_field(
            name="Treasury Discount",
            value=(
                f"Nation **{round(config.NATION_DISCOUNT * 100)}%** · "
                f"Kingdom **{round(config.KINGDOM_DISCOUNT * 100)}%** applied at checkout"
            ),
            inline=True,
        )
        embed.add_field(name="Contents", value="\n".join(lines)[:1024] or "_Empty_", inline=False)
        embed.add_field(name="Shulker Packaging", value=f"Included · {money(config.SHULKER_BOX_FEE)}", inline=True)
        embed.add_field(name="Created", value=kit["created_at"], inline=True)
        embed.set_footer(text="⚜ The Royal Court · Royal Bank of the Realm")
        return embed

    async def _can_spend(self, interaction: discord.Interaction, account_type: str, row) -> bool:
        if is_admin(interaction):
            return True
        return row["leader_id"] == interaction.user.id

    async def _publish_kit(self, guild_id: int, kit, kit_items: list, cmap: dict[str, dict]) -> None:
        settings = await self.db.get_settings(guild_id)
        channel_id = settings["kit_channel_id"]
        if not channel_id:
            return
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                self.bot.logger.warning("Kit channel %s is unavailable; kit #%s not published.",
                                        channel_id, kit["id"])
                return
        creator = self.bot.get_user(kit["creator_id"])
        if creator is None:
            try:
                creator = await self.bot.fetch_user(kit["creator_id"])
            except (discord.NotFound, discord.HTTPException):
                creator = None
        creator_name = creator.display_name if creator else f"<@{kit['creator_id']}>"
        embed = self._kit_embed(kit, kit_items, cmap, creator_name=creator_name)
        try:
            await channel.send(embed=embed)
        except Exception:
            self.bot.logger.exception("Failed to publish kit #%s", kit["id"])

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------
    @group.command(name="create", description="Create a kit (one per 7 days).")
    @app_commands.describe(name="A short name for your kit.")
    async def create(self, interaction: discord.Interaction, name: str):
        if interaction.guild_id is None:
            return await interaction.response.send_message(
                embed=embeds.error("Guild Only", "Kits only work inside a server."), ephemeral=True
            )
        name = name.strip()[:80]
        if not name:
            return await interaction.response.send_message(
                embed=embeds.error("Invalid Name", "Give your kit a name."), ephemeral=True
            )
        cd = await self.db.get_kit_cooldown(interaction.guild_id, interaction.user.id)
        if cd:
            last = parse_iso(cd["last_created_at"])
            if last:
                ready = last + dt.timedelta(days=config.KIT_COOLDOWN_DAYS)
                now = dt.datetime.now(dt.timezone.utc)
                if now < ready:
                    return await interaction.response.send_message(
                        embed=embeds.error(
                            "Cooldown",
                            f"You may create another kit <t:{int(ready.timestamp())}:R>.",
                        ),
                        ephemeral=True,
                    )
        await interaction.response.send_modal(KitItemsModal(self, name))

    async def _on_kit_submit(self, interaction: discord.Interaction, name: str, raw: str) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)

        lines = [ln for ln in raw.splitlines() if ln.strip()]
        if not lines:
            return await interaction.followup.send(
                embed=embeds.error("No Items", "List at least one item."), ephemeral=True
            )
        if len(lines) > config.KIT_MAX_ITEM_TYPES:
            return await interaction.followup.send(
                embed=embeds.error(
                    "Too Many Types",
                    f"A kit may contain at most **{config.KIT_MAX_ITEM_TYPES}** distinct item types.",
                ),
                ephemeral=True,
            )

        catalog = await self.db.list_catalog()
        banned = await self.db.banned_keys()

        # token -> resolved catalog row
        resolved: dict[str, dict] = {}
        for ln in lines:
            token, qty = _parse_kit_line(ln)
            if not token or qty <= 0:
                return await interaction.followup.send(
                    embed=embeds.error("Bad Line", f"Could not parse: `{ln}`"), ephemeral=True
                )
            row = await self._resolve_item(catalog, token)
            if not row:
                return await interaction.followup.send(
                    embed=embeds.error("Unknown Item", f"Could not resolve: `{token}`"), ephemeral=True
                )
            if pricing.is_banned(row["key"], banned):
                return await interaction.followup.send(
                    embed=embeds.error("Banned", f"**{row['display_name']}** cannot be sold."), ephemeral=True
                )
            # merge duplicate keys (still counts as one item type)
            existing = resolved.get(row["key"])
            if existing is None:
                resolved[row["key"]] = {"row": row, "qty": qty}
            else:
                resolved[row["key"]]["qty"] += qty

        if len(resolved) > config.KIT_MAX_ITEM_TYPES:
            return await interaction.followup.send(
                embed=embeds.error(
                    "Too Many Types",
                    f"A kit may contain at most **{config.KIT_MAX_ITEM_TYPES}** distinct item types.",
                ),
                ephemeral=True,
            )

        # Claim the 7-day slot and write the kit in one transaction. The
        # pre-check before the modal is only a courtesy; this guard is what
        # stops two modals submitted together from both creating a kit.
        now = dt.datetime.now(dt.timezone.utc)
        stamp = now.strftime("%Y-%m-%dT%H:%M:%S")
        ready_at = (now - dt.timedelta(days=config.KIT_COOLDOWN_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
        won = False
        async with self.db.transaction():
            if await self.db.claim_kit_cooldown(
                interaction.guild_id, interaction.user.id, stamp, ready_at
            ):
                won = True
                kit_id = await self.db.create_kit(
                    interaction.guild_id, name, interaction.user.id, kind="player"
                )
                for key, data in resolved.items():
                    await self.db.add_kit_item(kit_id, key, data["qty"])
        if not won:
            return await interaction.followup.send(
                embed=embeds.error(
                    "Cooldown",
                    f"You already created a kit in this {config.KIT_COOLDOWN_DAYS}-day window.",
                ),
                ephemeral=True,
            )

        kit = await self.db.get_kit(kit_id)
        kit_items = await self.db.get_kit_items(kit_id)
        cmap = await self._catalog_map()
        await self._publish_kit(interaction.guild_id, kit, kit_items, cmap)

        embed = self._kit_embed(kit, kit_items, cmap, creator_name=interaction.user.display_name)
        await interaction.followup.send(
            embed=embeds.success("Kit Created", f"**{name}** is now listed in the Public Kit Channel."),
            ephemeral=True,
        )
        # Show the full listing back to the creator privately.
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------
    # Browse / view
    # ------------------------------------------------------------------
    @group.command(name="list", description="List public kits.")
    async def list(self, interaction: discord.Interaction):
        kits = await self.db.list_kits(interaction.guild_id, status="active")
        if not kits:
            return await interaction.response.send_message(
                embed=embeds.info("No Kits", "No public kits have been created yet."), ephemeral=True
            )
        cmap = await self._catalog_map()
        lines = []
        for k in kits[:15]:
            kit_items = await self.db.get_kit_items(k["id"])
            items_total, _ = self._kit_summary(kit_items, cmap)
            price = self._kit_price(items_total)
            lines.append(f"`#{k['id']}` **{k['name']}** — {money(price)} · by <@{k['creator_id']}>")
        embed = discord.Embed(title="📦 Public Kits", description="\n".join(lines), color=config.GOLD_COLOR)
        embed.set_footer(
            text=(
                "Prices shown before treasury discount · Use /kit view <id> for contents"
                " · ⚜ The Royal Court"
            )
        )
        await interaction.response.send_message(embed=embed)

    @group.command(name="view", description="View a kit's contents and price.")
    @app_commands.describe(kit_id="The kit's ID.")
    async def view(self, interaction: discord.Interaction, kit_id: int):
        if interaction.guild_id is None:
            return await interaction.response.send_message(
                embed=embeds.error("Guild Only", "Kits only work inside a server."), ephemeral=True
            )
        kit = await self.db.get_kit(kit_id)
        if not kit or kit["status"] != "active" or int(kit["guild_id"]) != int(interaction.guild_id):
            return await interaction.response.send_message(
                embed=embeds.error("Not Found", "That kit does not exist."), ephemeral=True
            )
        kit_items = await self.db.get_kit_items(kit_id)
        cmap = await self._catalog_map()
        await interaction.response.send_message(embed=self._kit_embed(kit, kit_items, cmap))

    # ------------------------------------------------------------------
    # Buy
    # ------------------------------------------------------------------
    @group.command(name="buy", description="Buy a kit with a nation or kingdom treasury.")
    @app_commands.describe(kit_id="The kit to buy.", account="The treasury that pays.")
    @app_commands.autocomplete(account=account_autocomplete)
    async def buy(self, interaction: discord.Interaction, kit_id: int, account: str):
        if interaction.guild_id is None:
            return await interaction.response.send_message(
                embed=embeds.error("Guild Only", "Kits only work inside a server."), ephemeral=True
            )
        atype, arow = await resolve_account(self.db, interaction.guild_id, account)
        if not arow:
            return await interaction.response.send_message(
                embed=embeds.error("Not Found", "That account does not exist."), ephemeral=True
            )
        if not await self._can_spend(interaction, atype, arow):
            return await interaction.response.send_message(
                embed=embeds.error(
                    "Not Authorized",
                    f"Only the leader of {account_emoji(atype)} **{arow['name']}** "
                    f"(or an admin) may spend from it.",
                ),
                ephemeral=True,
            )
        kit = await self.db.get_kit(kit_id)
        if not kit or kit["status"] != "active" or int(kit["guild_id"]) != int(interaction.guild_id):
            return await interaction.response.send_message(
                embed=embeds.error("Not Found", "That kit does not exist."), ephemeral=True
            )
        kit_items = await self.db.get_kit_items(kit_id)
        union_member = await self.db.is_union_member(interaction.guild_id, atype, arow["id"])
        try:
            order, quote, components = await checkout_kit(
                self.bot,
                self.db,
                guild_id=interaction.guild_id,
                buyer_id=interaction.user.id,
                treasury_type=atype,
                treasury_row=arow,
                kit_row=kit,
                kit_items=kit_items,
                union_member=union_member,
            )
        except ShopError as exc:
            return await interaction.response.send_message(
                embed=embeds.error("Checkout Failed", str(exc)), ephemeral=True
            )

        items = await self.db.get_order_items(order["id"])
        embed = order_embed(order, items, account_name=f"{account_emoji(atype)} **{arow['name']}**", success=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        try:
            await interaction.user.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            await interaction.followup.send(
                embed=embeds.info("DMs Closed", "Your redeem key is shown in the message above."),
                ephemeral=True,
            )

    # ------------------------------------------------------------------
    # Admin controls
    # ------------------------------------------------------------------
    @group.command(name="setchannel", description="[Admin] Set the Public Kit Channel.")
    @app_commands.describe(channel="The channel where new kits are published.")
    @admin_only()
    async def setchannel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await self.db.set_kit_channel(interaction.guild_id, channel.id)
        await interaction.response.send_message(
            embed=embeds.success("Decree Sealed", f"New kits will be published in {channel.mention}."),
            ephemeral=True,
        )

    @group.command(name="resetcooldown", description="[Admin] Reset a player's kit cooldown.")
    @app_commands.describe(member="The player whose cooldown to reset.")
    @admin_only()
    async def resetcooldown(self, interaction: discord.Interaction, member: discord.Member):
        await self.db.reset_kit_cooldown(interaction.guild_id, member.id)
        await interaction.response.send_message(
            embed=embeds.success("Cooldown Reset", f"{member.mention} may now create another kit."),
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Kits(bot))
