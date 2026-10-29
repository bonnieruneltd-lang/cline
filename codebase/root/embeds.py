"""
embeds.py
=========
Themed embed builders for the Royal Court bot.
"""

from __future__ import annotations

import datetime as dt
from typing import Optional

import discord

import config
from payouts import PayoutResult
from utils import money


def _footer(embed: discord.Embed) -> discord.Embed:
    embed.set_footer(text="⚜ The Royal Court · Royal Bank of the Realm")
    embed.timestamp = dt.datetime.now(dt.timezone.utc)
    return embed


def success(title: str, description: str) -> discord.Embed:
    return _footer(discord.Embed(title=f"✅ {title}", description=description, color=config.GREEN_COLOR))


def error(title: str, description: str) -> discord.Embed:
    return _footer(discord.Embed(title=f"⛔ {title}", description=description, color=config.RED_COLOR))


def info(title: str, description: str) -> discord.Embed:
    return _footer(discord.Embed(title=f"ℹ️ {title}", description=description, color=config.BLUE_COLOR))


def payout_embed(title: str, result: PayoutResult, extra_lines: Optional[list[str]] = None) -> discord.Embed:
    embed = discord.Embed(title=f"👑 {title}", color=config.GOLD_COLOR)
    body = []
    for line in result.lines:
        # Hide zero-count rows for readability. The base allowance always has a
        # quantity of 1, so it is never hidden.
        if line.quantity == 0:
            continue
        body.append(f"`{line.quantity:>4} ×` **{money(line.rate)}** — {line.label} → {money(line.subtotal)}")
    if not body:
        body.append("_No qualifying stats yet._")
    embed.description = "\n".join(body)
    embed.add_field(name="Total Payout", value=f"## {money(result.total)}", inline=False)
    if extra_lines:
        embed.add_field(name="​", value="\n".join(extra_lines), inline=False)
    return _footer(embed)


def account_embed(
    account_type: str,
    row,
    *,
    next_payout: Optional[int] = None,
    member_count: Optional[int] = None,
) -> discord.Embed:
    emoji = "🏛️" if account_type == "nation" else "👑"
    label = "Nation" if account_type == "nation" else "Kingdom"
    embed = discord.Embed(title=f"{emoji} {label} · {row['name']}", color=config.BRAND_COLOR)

    leader = f"<@{row['leader_id']}>" if row["leader_id"] else "_unassigned_"
    embed.add_field(name="Leader", value=leader, inline=True)
    embed.add_field(name="Royal Bank Balance", value=f"**{money(row['balance'])}**", inline=True)

    if account_type == "nation":
        embed.add_field(name="Happy Workers", value=str(row["happy_workers"]), inline=True)
        embed.add_field(name="Regular Workers", value=str(row["regular_workers"]), inline=True)
        embed.add_field(name="Happy Soldiers", value=str(row["happy_soldiers"]), inline=True)
        embed.add_field(name="Regular Soldiers", value=str(row["regular_soldiers"]), inline=True)
    else:
        embed.add_field(name="Member Nations", value=str(member_count if member_count is not None else "—"), inline=True)

    if next_payout is not None:
        embed.add_field(name="Next Payout (this cycle)", value=f"**{money(next_payout)}**", inline=False)
    return _footer(embed)
