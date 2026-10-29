"""
cogs/leaderboard.py
===================
Leaderboards for the three economy types.

They are deliberately separate commands: a nation, a kingdom and an individual
are different ledgers with different caps and rules, so their rankings are
never merged into a single list.
"""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

import config
import embeds
from utils import money

MEDALS = ("🥇", "🥈", "🥉")

_QUERIES = {
    "nation": """SELECT name AS label, balance FROM nations
                 WHERE guild_id = ?
                 ORDER BY balance DESC, name COLLATE NOCASE ASC
                 LIMIT ?""",
    "kingdom": """SELECT name AS label, balance FROM kingdoms
                  WHERE guild_id = ?
                  ORDER BY balance DESC, name COLLATE NOCASE ASC
                  LIMIT ?""",
    "individual": """SELECT '<@' || discord_id || '>' AS label, balance FROM individuals
                     WHERE guild_id = ?
                     ORDER BY balance DESC, discord_id ASC
                     LIMIT ?""",
}


class Leaderboard(commands.Cog):
    """🏆 Separate rankings for nations, kingdoms and individuals."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @property
    def db(self):
        return self.bot.db  # type: ignore[attr-defined]

    group = app_commands.Group(name="leaderboard", description="🏆 Rankings for each economy type.")

    async def top(self, kind: str, guild_id: int, limit: int | None = None) -> list:
        """Top rows for one economy type. Never mixes the three tables."""
        assert kind in _QUERIES
        return await self.db.fetchall(
            _QUERIES[kind], (guild_id, limit or config.LEADERBOARD_LIMIT)
        )

    def render(self, kind: str, rows: list) -> discord.Embed:
        title = {
            "nation": "🏛️ Nation Leaderboard",
            "kingdom": "👑 Kingdom Leaderboard",
            "individual": "👤 Individual Leaderboard",
        }[kind]
        color = config.BRAND_COLOR if kind == "individual" else config.GOLD_COLOR
        lines = []
        for index, row in enumerate(rows):
            marker = MEDALS[index] if index < len(MEDALS) else f"`#{index + 1}`"
            lines.append(f"{marker} **{row['label']}** — {money(int(row['balance']))}")
        embed = discord.Embed(title=title, description="\n".join(lines), color=color)
        embed.set_footer(
            text=f"⚜ The Royal Court · {kind.capitalize()} ledger · never combined with the other two"
        )
        return embed

    async def _send(self, interaction: discord.Interaction, kind: str) -> None:
        if interaction.guild_id is None:
            return await interaction.response.send_message(
                embed=embeds.error("Guild Only", "Leaderboards only work inside a server."),
                ephemeral=True,
            )
        rows = await self.top(kind, interaction.guild_id)
        if not rows:
            return await interaction.response.send_message(
                embed=embeds.info(
                    f"No {kind.capitalize()}s",
                    f"No {kind} balances exist yet.",
                ),
                ephemeral=True,
            )
        await interaction.response.send_message(embed=self.render(kind, rows))

    @group.command(name="nation", description="Top nations by Royal Bank balance.")
    async def nation(self, interaction: discord.Interaction):
        await self._send(interaction, "nation")

    @group.command(name="kingdom", description="Top kingdoms by Royal Bank balance.")
    async def kingdom(self, interaction: discord.Interaction):
        await self._send(interaction, "kingdom")

    @group.command(name="individual", description="Top individual balances.")
    async def individual(self, interaction: discord.Interaction):
        await self._send(interaction, "individual")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Leaderboard(bot))
