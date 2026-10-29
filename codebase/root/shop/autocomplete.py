"""Discord autocomplete helpers for the shop catalog."""
from __future__ import annotations

import discord
from discord import app_commands

from shop import pricing
from utils import money


def _shorten(text: str, limit: int = 96) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def item_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    db = interaction.client.db
    banned = await db.banned_keys()
    lowered = current.lower()
    choices: list[app_commands.Choice[str]] = []
    for row in await db.list_catalog():
        if pricing.is_banned(row["key"], banned):
            continue
        if lowered in row["display_name"].lower() or lowered in row["key"].lower():
            choices.append(
                app_commands.Choice(
                    name=_shorten(f"{row['display_name']} — {money(row['price'])}"),
                    value=row["key"],
                )
            )
    return choices[:25]


async def category_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    db = interaction.client.db
    lowered = current.lower()
    cats = await db.list_catalog_categories()
    return [app_commands.Choice(name=c, value=c) for c in cats if lowered in c.lower()][:25]


async def banned_item_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Autocomplete over currently banned keys (for admin unban)."""
    db = interaction.client.db
    lowered = current.lower()
    choices: list[app_commands.Choice[str]] = []
    for key in sorted(await db.banned_keys()):
        row = await db.get_catalog_item(key)
        name = row["display_name"] if row else key
        if lowered in name.lower() or lowered in key.lower():
            choices.append(app_commands.Choice(name=_shorten(name), value=key))
    return choices[:25]
