"""
cogs/shop.py
============
The Royal Shop — the data-driven item catalog.

Purchases are funded exclusively from a Nation or Kingdom treasury. Checkout
generates a single-use redeem key (RC-XXXXXX-XXXXXX) that is delivered to the
buyer via Discord DM; the key is later redeemed in-game by the Forge mod.

No Minecraft account linking is used: the redeem key is the credential.
"""
from __future__ import annotations

import math

import discord
from discord import app_commands
from discord.ext import commands

import config
import embeds
from checks import admin_only
from shop import pricing
from shop.autocomplete import banned_item_autocomplete, category_autocomplete, item_autocomplete
from shop.checkout import ShopError, checkout
from shop.embeds import order_embed
from utils import account_autocomplete, account_emoji, is_admin, money, resolve_account


PER_PAGE = 10


class CatalogView(discord.ui.View):
    """Paginated, non-persistent catalog browser."""

    def __init__(self, rows: list, *, category: str | None = None) -> None:
        super().__init__(timeout=180)
        self.rows = rows
        self.category = category
        self.page = 0
        self._update_buttons()

    def _pages(self) -> int:
        return max(1, math.ceil(len(self.rows) / PER_PAGE))

    def _update_buttons(self) -> None:
        pages = self._pages()
        self.prev.disabled = self.page == 0
        self.next.disabled = self.page >= pages - 1

    def _render(self) -> discord.Embed:
        pages = self._pages()
        start = self.page * PER_PAGE
        chunk = self.rows[start : start + PER_PAGE]
        lines = []
        for r in chunk:
            qty = f"`{r['unit_qty']}×`" if r["stackable"] else "`1×`"
            lines.append(f"**{r['display_name']}** — {money(r['price'])} {qty}\n`{r['key']}`")
        title = "🛒 Royal Shop" + (f" · {self.category}" if self.category else "")
        embed = discord.Embed(title=title, description="\n".join(lines) or "_No items._", color=config.BRAND_COLOR)
        embed.set_footer(text=f"Page {self.page + 1}/{pages} · {len(self.rows)} item(s) · ⚜ The Royal Court")
        return embed

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page = max(0, self.page - 1)
        self._update_buttons()
        await interaction.response.edit_message(embed=self._render(), view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page = min(self._pages() - 1, self.page + 1)
        self._update_buttons()
        await interaction.response.edit_message(embed=self._render(), view=self)


class Shop(commands.Cog):
    """🛒 The Royal Shop — buy items with treasury credits."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @property
    def db(self):
        return self.bot.db  # type: ignore[attr-defined]

    group = app_commands.Group(name="shop", description="🛒 The Royal Shop of the realm.")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _can_spend(self, interaction: discord.Interaction, account_type: str, row) -> bool:
        """Admin OR the leader of the paying treasury."""
        if is_admin(interaction):
            return True
        return row["leader_id"] == interaction.user.id

    def _order_embed(
        self,
        order,
        items: list,
        *,
        account_name: str | None = None,
        success: bool = False,
    ) -> discord.Embed:
        return order_embed(order, items, account_name=account_name, success=success)

    async def _dm_order(self, user: discord.User, embed: discord.Embed) -> bool:
        """Send the order key to the buyer's DMs. Returns True on success."""
        try:
            await user.send(embed=embed)
            return True
        except (discord.Forbidden, discord.HTTPException):
            return False

    # ------------------------------------------------------------------
    # Browse / item
    # ------------------------------------------------------------------
    @group.command(name="browse", description="Browse the Royal Shop catalog.")
    @app_commands.describe(category="Optional category filter.")
    @app_commands.autocomplete(category=category_autocomplete)
    async def browse(self, interaction: discord.Interaction, category: str | None = None):
        rows = await self.db.list_catalog(category)
        banned = await self.db.banned_keys()
        rows = [r for r in rows if not pricing.is_banned(r["key"], banned)]
        if not rows:
            return await interaction.response.send_message(
                embed=embeds.info("Nothing Here", "No items match that filter."), ephemeral=True
            )
        view = CatalogView(rows, category=category)
        await interaction.response.send_message(embed=view._render(), view=view)

    @group.command(name="item", description="View a single shop item.")
    @app_commands.describe(item="The item to inspect.")
    @app_commands.autocomplete(item=item_autocomplete)
    async def item(self, interaction: discord.Interaction, item: str):
        row = await self.db.get_catalog_item(item)
        if not row:
            return await interaction.response.send_message(
                embed=embeds.error("Not Found", "That item is not in the catalog."), ephemeral=True
            )
        banned = await self.db.banned_keys()
        if pricing.is_banned(row["key"], banned):
            return await interaction.response.send_message(
                embed=embeds.error("Banned", f"**{row['display_name']}** cannot be sold."), ephemeral=True
            )
        embed = discord.Embed(title=f"🛒 {row['display_name']}", color=config.BRAND_COLOR)
        embed.add_field(name="Price", value=f"{money(row['price'])}", inline=True)
        embed.add_field(name="Quantity per unit", value=str(row["unit_qty"]), inline=True)
        embed.add_field(name="Stackable", value="Yes" if row["stackable"] else "No", inline=True)
        embed.add_field(name="Category", value=row["category"], inline=True)
        embed.add_field(name="Item Key", value=f"`{row['key']}`", inline=False)
        embed.set_footer(text="⚜ The Royal Court · Royal Bank of the Realm")
        await interaction.response.send_message(embed=embed)

    # ------------------------------------------------------------------
    # Buy
    # ------------------------------------------------------------------
    @group.command(name="buy", description="Buy a shop item using a nation or kingdom treasury.")
    @app_commands.describe(
        account="The treasury that pays.", item="The item to buy.", quantity="Number of catalog units."
    )
    @app_commands.autocomplete(account=account_autocomplete, item=item_autocomplete)
    async def buy(self, interaction: discord.Interaction, account: str, item: str, quantity: int):
        atype, arow = await resolve_account(self.db, interaction.guild_id, account)
        if not arow:
            return await interaction.response.send_message(
                embed=embeds.error("Not Found", "That account does not exist."), ephemeral=True
            )
        if not self._can_spend(interaction, atype, arow):
            return await interaction.response.send_message(
                embed=embeds.error(
                    "Not Authorized",
                    f"Only the leader of {account_emoji(atype)} **{arow['name']}** "
                    f"(or an admin) may spend from it.",
                ),
                ephemeral=True,
            )
        if quantity <= 0:
            return await interaction.response.send_message(
                embed=embeds.error("Invalid Quantity", "Quantity must be greater than zero."), ephemeral=True
            )
        row = await self.db.get_catalog_item(item)
        if not row:
            return await interaction.response.send_message(
                embed=embeds.error("Not Found", "That item is not in the catalog."), ephemeral=True
            )

        union_member = await self.db.is_union_member(interaction.guild_id, atype, arow["id"])
        try:
            order, quote, components = await checkout(
                self.bot,
                self.db,
                guild_id=interaction.guild_id,
                buyer_id=interaction.user.id,
                treasury_type=atype,
                treasury_row=arow,
                lines=[(row, quantity)],
                union_member=union_member,
            )
        except ShopError as exc:
            return await interaction.response.send_message(
                embed=embeds.error("Checkout Failed", str(exc)), ephemeral=True
            )

        items = await self.db.get_order_items(order["id"])
        embed = self._order_embed(order, items, account_name=f"{account_emoji(atype)} **{arow['name']}**", success=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

        if not await self._dm_order(interaction.user, embed):
            await interaction.followup.send(
                embed=embeds.info(
                    "DMs Closed",
                    "I couldn't DM you. Your redeem key is shown in the message above — keep it safe.",
                ),
                ephemeral=True,
            )

    # ------------------------------------------------------------------
    # Order lookup
    # ------------------------------------------------------------------
    @group.command(name="orders", description="List your shop orders (or another member's, if admin).")
    @app_commands.describe(member="[Admin] A member whose orders to list.")
    async def orders(self, interaction: discord.Interaction, member: discord.Member | None = None):
        if interaction.guild_id is None:
            return await interaction.response.send_message(
                embed=embeds.error("Guild Only", "Shop orders live inside a server."), ephemeral=True
            )
        target = member or interaction.user
        if member is not None and member.id != interaction.user.id and not is_admin(interaction):
            return await interaction.response.send_message(
                embed=embeds.error("Not Authorized", "Only admins may view another member's orders."),
                ephemeral=True,
            )
        rows = await self.db.list_orders(interaction.guild_id, target.id)
        if not rows:
            return await interaction.response.send_message(
                embed=embeds.info("No Orders", f"{target.display_name} has no shop orders."), ephemeral=True
            )
        lines = []
        for r in rows[:15]:
            status = "✅ redeemed" if r["status"] == "redeemed" else "⏳ pending"
            lines.append(f"`{r['redeem_key']}` — {money(r['final_total'])} ({status})")
        embed = discord.Embed(
            title=f"🧾 Orders · {target.display_name}",
            description="\n".join(lines),
            color=config.BRAND_COLOR,
        )
        embed.set_footer(text="⚜ The Royal Court · Royal Bank of the Realm")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @group.command(name="order", description="View a shop order by its redeem key.")
    @app_commands.describe(redeem_key="The order's redeem key (e.g. RC-XXXXXX-XXXXXX).")
    async def order(self, interaction: discord.Interaction, redeem_key: str):
        if interaction.guild_id is None:
            return await interaction.response.send_message(
                embed=embeds.error("Guild Only", "Shop orders live inside a server."), ephemeral=True
            )
        row = await self.db.get_order_by_key(redeem_key.strip().upper())
        if not row or int(row["guild_id"]) != int(interaction.guild_id):
            return await interaction.response.send_message(
                embed=embeds.error("Not Found", "No order matches that redeem key."), ephemeral=True
            )
        if row["buyer_id"] != interaction.user.id and not is_admin(interaction):
            return await interaction.response.send_message(
                embed=embeds.error("Not Authorized", "You can only view your own orders."), ephemeral=True
            )
        items = await self.db.get_order_items(row["id"])
        embed = self._order_embed(row, items)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------
    # Admin controls
    # ------------------------------------------------------------------
    @group.command(name="ban", description="[Admin] Ban an item from the shop.")
    @app_commands.describe(item="The item to ban.", reason="Why it is banned.")
    @app_commands.autocomplete(item=item_autocomplete)
    @admin_only()
    async def ban(self, interaction: discord.Interaction, item: str, reason: str | None = None):
        row = await self.db.get_catalog_item(item)
        if not row:
            return await interaction.response.send_message(
                embed=embeds.error("Not Found", "That item is not in the catalog."), ephemeral=True
            )
        await self.db.ban_item(row["key"], reason, interaction.user.id)
        await interaction.response.send_message(
            embed=embeds.success("Item Banned", f"**{row['display_name']}** (`{row['key']}`) is now banned."),
            ephemeral=True,
        )

    @group.command(name="unban", description="[Admin] Remove a ban from an item.")
    @app_commands.describe(item="The item to unban.")
    @app_commands.autocomplete(item=banned_item_autocomplete)
    @admin_only()
    async def unban(self, interaction: discord.Interaction, item: str):
        await self.db.unban_item(item)
        await interaction.response.send_message(
            embed=embeds.success("Item Unbanned", f"`{item}` is available again."), ephemeral=True
        )

    @group.command(name="setprice", description="[Admin] Change an item's price.")
    @app_commands.describe(item="The item.", price="New price in credits.")
    @app_commands.autocomplete(item=item_autocomplete)
    @admin_only()
    async def setprice(self, interaction: discord.Interaction, item: str, price: int):
        row = await self.db.get_catalog_item(item)
        if not row:
            return await interaction.response.send_message(
                embed=embeds.error("Not Found", "That item is not in the catalog."), ephemeral=True
            )
        if price < 0:
            return await interaction.response.send_message(
                embed=embeds.error("Invalid Price", "Price cannot be negative."), ephemeral=True
            )
        await self.db.set_catalog_price(row["key"], price)
        await interaction.response.send_message(
            embed=embeds.success(
                "Price Updated",
                f"**{row['display_name']}** is now **{money(price)}** (was {money(row['price'])}).",
            ),
            ephemeral=True,
        )

    @group.command(name="markredeemed", description="[Admin] Manually mark an order redeemed (Forge mod fallback).")
    @app_commands.describe(redeem_key="The order's redeem key.", minecraft_uuid="The in-game player UUID.")
    @admin_only()
    async def markredeemed(self, interaction: discord.Interaction, redeem_key: str, minecraft_uuid: str):
        if interaction.guild_id is None:
            return await interaction.response.send_message(
                embed=embeds.error("Guild Only", "Shop orders live inside a server."), ephemeral=True
            )
        row = await self.db.get_order_by_key(redeem_key.strip().upper())
        if not row or int(row["guild_id"]) != int(interaction.guild_id):
            return await interaction.response.send_message(
                embed=embeds.error("Not Found", "No order matches that redeem key."), ephemeral=True
            )
        if not await self.db.redeem_order(row["id"], minecraft_uuid.strip()):
            return await interaction.response.send_message(
                embed=embeds.error("Already Redeemed", f"Order #{row['id']} was already redeemed."), ephemeral=True
            )
        await interaction.response.send_message(
            embed=embeds.success("Order Redeemed", f"Order #{row['id']} marked as redeemed for `{minecraft_uuid.strip()}`."),
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Shop(bot))
