"""
cogs/ledger.py
==============
The Royal Ledger — full audit history of every money movement & change.

The user asked to "log everything" so admins can verify whether payouts
actually happened in-game and manage the realm.
"""

from __future__ import annotations

import io
import csv
import datetime as dt

import discord
from discord import app_commands
from discord.ext import commands

import embeds
from checks import admin_only
from database import parse_iso
from utils import account_autocomplete, resolve_account

KIND_EMOJI = {
    "payout": "⚜",
    "deposit": "📥",
    "withdraw": "📤",
    "transfer": "🔁",
    "setbalance": "✏️",
    "draw": "🏦",
    "draw_denied": "🚫",
    "nation_create": "🏛️",
    "nation_delete": "🗑️",
    "nation_setleader": "👤",
    "nation_stats": "📊",
    "nation_request": "📜",
    "nation_request_denied": "🚫",
    "kingdom_create": "👑",
    "kingdom_delete": "🗑️",
    "kingdom_setleader": "👤",
    "kingdom_addnation": "➕",
    "kingdom_removenation": "➖",
    "kingdom_request": "📜",
    "kingdom_request_denied": "🚫",
    "config": "⚙️",
    "union_fund": "⚒️",
    "union_donate": "🤝",
    "union_join": "📝",
    "union_leave": "🚪",
    "union_tax": "🧾",
    "crown_tax": "👑",
    "crown_supplier": "📦",
    "crown_fund": "🏦",
    "crown_withdraw": "🏦",
    "faction_donate": "🤝",
    "contract_grant": "💰",
    "contract_release": "🔓",
    "contract_reject": "🚫",
    "shop_purchase": "🛒",
    "kit_purchase": "📦",
    "individual_coinflip": "🪙",
    "individual_allin": "🎰",
    "individual_blackjack": "🃏",
    "fishing_catch": "🎣",
    "fishing_guild_fund": "🏦",
    "fishing_guild_withdraw": "🏦",
    "casino_fund": "🏦",
    "casino_withdraw": "🏦",
}


class Ledger(commands.Cog):
    """📖 The Royal Ledger — audit history of the realm."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @property
    def db(self):
        return self.bot.db  # type: ignore[attr-defined]

    group = app_commands.Group(name="ledger", description="📖 The Royal Ledger — audit history.")

    @group.command(name="view", description="View the recent transaction history for an account.")
    @app_commands.describe(account="The nation or kingdom.", limit="How many entries (default 15, max 25).")
    @app_commands.autocomplete(account=account_autocomplete)
    async def view(self, interaction: discord.Interaction, account: str, limit: app_commands.Range[int, 1, 25] = 15):
        atype, row = await resolve_account(self.db, interaction.guild_id, account)
        if not row:
            return await interaction.response.send_message(
                embed=embeds.error("Not Found", "That account does not exist."), ephemeral=True
            )
        rows = await self.db.recent_transactions(
            interaction.guild_id, limit=limit, account_type=atype, account_id=row["id"]
        )
        if not rows:
            return await interaction.response.send_message(
                embed=embeds.info("No History", f"No transactions recorded for **{row['name']}** yet."),
                ephemeral=True,
            )
        embed = discord.Embed(
            title=f"📖 Ledger · {'🏛️' if atype == 'nation' else '👑'} {row['name']}",
            color=0x8B0000,
        )
        for tx in rows:
            emoji = KIND_EMOJI.get(tx["kind"], "•")
            ts = parse_iso(tx["ts"])
            stamp = f"<t:{int(ts.timestamp())}:R>" if ts else ""
            sign = "+" if tx["amount"] > 0 else ""
            detail = f"{sign}{tx['amount']:,}" if tx["amount"] else "—"
            embed.add_field(
                name=f"{emoji} {tx['kind']} · {detail}",
                value=f"{tx['note'] or '—'}\n{stamp}",
                inline=False,
            )
        embed.set_footer(text="⚜ The Royal Court · Royal Bank of the Realm")
        await interaction.response.send_message(embed=embed)

    @group.command(name="all", description="[Admin] View the server-wide transaction history.")
    @app_commands.describe(limit="How many entries (default 20, max 25).")
    @admin_only()
    async def all_tx(self, interaction: discord.Interaction, limit: app_commands.Range[int, 1, 25] = 20):
        rows = await self.db.recent_transactions(interaction.guild_id, limit=limit)
        if not rows:
            return await interaction.response.send_message(
                embed=embeds.info("No History", "No transactions recorded yet."), ephemeral=True
            )
        embed = discord.Embed(title="📖 Royal Ledger · Server-Wide", color=0x8B0000)
        for tx in rows:
            emoji = KIND_EMOJI.get(tx["kind"], "•")
            ts = parse_iso(tx["ts"])
            stamp = f"<t:{int(ts.timestamp())}:R>" if ts else ""
            who = tx["account_name"] or "—"
            sign = "+" if tx["amount"] > 0 else ""
            detail = f"{sign}{tx['amount']:,}" if tx["amount"] else "—"
            embed.add_field(
                name=f"{emoji} {tx['kind']} · {who} · {detail}",
                value=f"{tx['note'] or '—'}\n{stamp}",
                inline=False,
            )
        embed.set_footer(text="⚜ The Royal Court · Royal Bank of the Realm")
        await interaction.response.send_message(embed=embed)

    @group.command(name="export", description="[Admin] Export the full ledger to a CSV file.")
    @admin_only()
    async def export(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True, ephemeral=True)
        rows = await self.db.all_transactions(interaction.guild_id)
        if not rows:
            return await interaction.followup.send(
                embed=embeds.info("No History", "No transactions recorded yet."), ephemeral=True
            )
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "id", "timestamp", "kind", "account_type", "account_name",
            "counterparty_type", "counterparty_name", "amount",
            "balance_after", "actor_id", "note",
        ])
        for tx in rows:
            writer.writerow([
                tx["id"], tx["ts"], tx["kind"], tx["account_type"], tx["account_name"],
                tx["counterparty_type"], tx["counterparty_name"], tx["amount"],
                tx["balance_after"], tx["actor_id"], tx["note"],
            ])
        data = buf.getvalue().encode("utf-8")
        file = discord.File(io.BytesIO(data), filename=f"royal_ledger_{dt.date.today().isoformat()}.csv")
        await interaction.followup.send(
            content=f"📖 Full ledger export ({len(rows)} entries).", file=file, ephemeral=True
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Ledger(bot))
