"""
shop/embeds.py
==============
Shared embed builder for shop orders, used by the shop and kit cogs.
"""
from __future__ import annotations

import discord

import config
from utils import money


def order_embed(
    order,
    items: list,
    *,
    account_name: str | None = None,
    success: bool = False,
) -> discord.Embed:
    status = "✅ Redeemed" if order["status"] == "redeemed" else "⏳ Pending delivery"
    embed = discord.Embed(
        title=f"{'✅ Order Confirmed' if success else '🧾 Order'} #{order['id']}",
        color=config.GREEN_COLOR if success else config.BRAND_COLOR,
    )
    embed.add_field(name="Status", value=status, inline=True)
    embed.add_field(name="Buyer", value=f"<@{order['buyer_id']}>", inline=True)
    if account_name:
        embed.add_field(name="Charged To", value=account_name, inline=True)
    embed.add_field(
        name="🎟 Redeem Key",
        value=f"```{order['redeem_key']}```Redeem this in-game with the Forge mod.",
        inline=False,
    )
    lines = [f"`{it['quantity']:>5} ×` **{it['display_name']}**" for it in items]
    embed.add_field(name="Contents", value="\n".join(lines)[:1024] or "_None_", inline=False)
    breakdown = f"Items: **{money(order['items_total'])}**"
    if order["shulker_fee"]:
        breakdown += f"\nShulker fee: **{money(order['shulker_fee'])}**"
    union_discount = order["union_discount"] if "union_discount" in order.keys() else 0
    if union_discount:
        breakdown += f"\nUnion member discount: **−{money(union_discount)}**"
    treasury_discount = order["discount"] - union_discount
    if treasury_discount:
        breakdown += f"\nTreasury discount: **−{money(treasury_discount)}**"
    breakdown += f"\n**Total: {money(order['final_total'])}**"
    embed.add_field(name="Price", value=breakdown, inline=False)
    embed.add_field(name="Created", value=order["created_at"], inline=True)
    embed.set_footer(text="⚜ The Royal Court · Royal Bank of the Realm")
    return embed
