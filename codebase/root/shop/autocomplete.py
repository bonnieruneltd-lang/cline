"""Discord autocomplete helpers for the shop catalog."""
from __future__ import annotations

import discord
from discord import app_commands

from shop import pricing
from utils import autocomplete_snapshot, money


def _shorten(text: str, limit: int = 96) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def _catalog(db):
    return await autocomplete_snapshot(("catalog",), db.list_catalog)


async def _banned(db):
    return await autocomplete_snapshot(("catalog_banned",), db.banned_keys)


async def item_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    db = interaction.client.db
    banned = await _banned(db)
    lowered = current.lower()
    choices: list[app_commands.Choice[str]] = []
    for row in await _catalog(db):
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
    cats = await autocomplete_snapshot(("catalog_categories",), db.list_catalog_categories)
    return [app_commands.Choice(name=c, value=c) for c in cats if lowered in c.lower()][:25]


async def banned_item_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Autocomplete over currently banned keys (for admin unban)."""
    db = interaction.client.db
    lowered = current.lower()
    choices: list[app_commands.Choice[str]] = []
    catalog = {row["key"]: row for row in await _catalog(db)}
    for key in sorted(await _banned(db)):
        row = catalog.get(key)
        name = row["display_name"] if row else key
        if lowered in name.lower() or lowered in key.lower():
            choices.append(app_commands.Choice(name=_shorten(name), value=key))
    return choices[:25]
