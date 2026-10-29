"""
utils.py
========
Shared helpers: money formatting, account resolution, autocomplete,
and permission checks.
"""

from __future__ import annotations

from typing import Optional

import discord
from discord import app_commands

import account_transfers
import config


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------
def money(amount: int) -> str:
    return f"{amount:,} {config.CURRENCY_NAME}"


def money_short(amount: int) -> str:
    return f"{config.CURRENCY_SYMBOL} {amount:,}"


# Shares a transfer may ask for instead of typing a number.
AMOUNT_SHARES = {"all": 1, "half": 2, "quarter": 4}


def parse_amount(value: str, available: int) -> int:
    """Resolve ``all``/``half``/``quarter`` or a plain amount against a balance.

    Raises ValueError with a user-facing message when the input is unusable.
    """
    text = (value or "").strip().lower().replace(",", "").replace("_", "")
    if text in AMOUNT_SHARES:
        share = AMOUNT_SHARES[text]
        if available <= 0:
            raise ValueError("That account is empty.")
        amount = available if share == 1 else available // share
        if amount <= 0:
            raise ValueError(f"There is not enough for {text} of that balance.")
        return amount
    try:
        amount = int(text)
    except ValueError:
        raise ValueError(
            "Amount must be a whole number, or **all**, **half** or **quarter**."
        ) from None
    if amount <= 0:
        raise ValueError("Amount must be greater than zero.")
    return amount


# ---------------------------------------------------------------------------
# Account resolution
# ---------------------------------------------------------------------------
# An "account" is encoded as a string: "nation:<Name>" or "kingdom:<Name>".
# This lets a single autocompleted parameter address either kind of account.

def encode_account(account_type: str, name: str) -> str:
    return f"{account_type}:{name}"


def parse_account(value: str) -> tuple[Optional[str], Optional[str]]:
    if not value or ":" not in value:
        return None, None
    account_type, _, name = value.partition(":")
    account_type = account_type.strip().lower()
    name = name.strip()
    if account_type not in ("nation", "kingdom") or not name:
        return None, None
    return account_type, name


async def resolve_account(db, guild_id: int, value: str):
    """
    Returns (account_type, row) or (None, None) if not found.
    `row` is the aiosqlite.Row for the nation or kingdom.
    """
    account_type, name = parse_account(value)
    if account_type is None:
        return None, None
    if account_type == "nation":
        return account_type, await db.get_nation(guild_id, name)
    return account_type, await db.get_kingdom(guild_id, name)


async def account_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Autocomplete listing both nations and kingdoms."""
    db = interaction.client.db  # type: ignore[attr-defined]
    if interaction.guild_id is None:
        return []
    current_l = current.lower()
    choices: list[app_commands.Choice[str]] = []

    for n in await db.list_nations(interaction.guild_id):
        label = f"🏛️ Nation · {n['name']}"
        if current_l in label.lower():
            choices.append(app_commands.Choice(name=label[:100], value=encode_account("nation", n["name"])))
    for k in await db.list_kingdoms(interaction.guild_id):
        label = f"👑 Kingdom · {k['name']}"
        if current_l in label.lower():
            choices.append(app_commands.Choice(name=label[:100], value=encode_account("kingdom", k["name"])))
    return choices[:25]


async def any_account_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Autocomplete every economy account: treasuries and individual wallets.

    Treasuries are listed first so a large wallet list cannot crowd them out of
    the 25-choice limit. Individuals are addressed by Discord id, which stays
    stable if a wallet is erased and recreated.
    """
    db = interaction.client.db  # type: ignore[attr-defined]
    if interaction.guild_id is None:
        return []
    current_l = current.lower()
    choices: list[app_commands.Choice[str]] = []

    for n in await db.list_nations(interaction.guild_id):
        label = f"🏛️ Nation · {n['name']}"
        if current_l in label.lower():
            choices.append(
                app_commands.Choice(
                    name=label[:100],
                    value=account_transfers.encode_ref(
                        account_transfers.NATION, n["name"]
                    ),
                )
            )
    for k in await db.list_kingdoms(interaction.guild_id):
        label = f"👑 Kingdom · {k['name']}"
        if current_l in label.lower():
            choices.append(
                app_commands.Choice(
                    name=label[:100],
                    value=account_transfers.encode_ref(
                        account_transfers.KINGDOM, k["name"]
                    ),
                )
            )
    for row in await db.list_individuals(interaction.guild_id):
        name = row["display_name"] or f"Member {row['discord_id']}"
        label = f"👤 {name}"
        if current_l in label.lower() or str(row["discord_id"]) in current:
            choices.append(
                app_commands.Choice(
                    name=label[:100],
                    value=account_transfers.encode_ref(
                        account_transfers.INDIVIDUAL, str(row["discord_id"])
                    ),
                )
            )
    return choices[:25]


async def nation_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    db = interaction.client.db  # type: ignore[attr-defined]
    if interaction.guild_id is None:
        return []
    current_l = current.lower()
    out = []
    for n in await db.list_nations(interaction.guild_id):
        if current_l in n["name"].lower():
            out.append(app_commands.Choice(name=n["name"][:100], value=n["name"]))
    return out[:25]


async def kingdom_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    db = interaction.client.db  # type: ignore[attr-defined]
    if interaction.guild_id is None:
        return []
    current_l = current.lower()
    out = []
    for k in await db.list_kingdoms(interaction.guild_id):
        if current_l in k["name"].lower():
            out.append(app_commands.Choice(name=k["name"][:100], value=k["name"]))
    return out[:25]


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------
def is_admin(interaction: discord.Interaction) -> bool:
    """Admins are determined by Discord's built-in administrator permission."""
    perms = getattr(interaction.user, "guild_permissions", None)
    return bool(perms and perms.administrator)


def is_nation_leader(user_id: int, nation_row) -> bool:
    return nation_row is not None and nation_row["leader_id"] == user_id


def is_kingdom_leader(user_id: int, kingdom_row) -> bool:
    return kingdom_row is not None and kingdom_row["leader_id"] == user_id


def account_type_label(account_type: str) -> str:
    return "Nation" if account_type == "nation" else "Kingdom"


def account_emoji(account_type: str) -> str:
    return "🏛️" if account_type == "nation" else "👑"
