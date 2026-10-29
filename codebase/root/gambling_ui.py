"""
gambling_ui.py
==============
Shared spectator-facing presentation for the public gambling tables.

Every result is posted in the channel, so the embeds are written for the people
watching as much as for the player: big result words, a readable table, payout
and wallet figures, and a visible invitation to react. No money logic lives
here. The same module renders the public Texas Hold'em table and its showdown
card.
"""
from __future__ import annotations

import discord

import config
from utils import money

SPECTATOR_HINT = (
    "👀 **The table is watching.** Spectators can react with "
    "🎉 to cheer a win or 💀 for a bust — the next hand is only a command away."
)
CASINO_FOOTER = "⚜ The Royal Court · Casino floor · every wager is public"


def _footer(embed: discord.Embed, text: str = CASINO_FOOTER) -> discord.Embed:
    embed.set_footer(text=text)
    return embed


def coinflip_result(
    *,
    user_mention: str,
    called: str,
    landed: str,
    stake: int,
    gross: int,
    balance: int,
    house: int,
    won: bool,
) -> discord.Embed:
    """A theatrical but compact coinflip result for the channel."""
    title = "🪙 COINFLIP · You Win" if won else "🪙 COINFLIP · You Lose"
    color = config.GOLD_COLOR if won else config.RED_COLOR
    net = gross - stake if won else -stake
    embed = discord.Embed(
        title=title,
        description=(
            f"## {landed.title()}!\n"
            f"{user_mention} called **{called}** and the coin landed "
            f"**{landed}**.\n\n"
            + (
                f"The Casino pays **{money(gross)}** across the table."
                if won
                else "The stake slides into the Casino's stack."
            )
        ),
        color=color,
    )
    embed.add_field(name="Stake", value=money(stake), inline=True)
    embed.add_field(name="Return", value=money(gross) if won else "—", inline=True)
    embed.add_field(name="Net", value=f"{net:+,}", inline=True)
    embed.add_field(name="Player Wallet", value=money(balance), inline=True)
    embed.add_field(name="Casino Stack", value=money(house), inline=True)
    embed.add_field(name="The Floor", value=SPECTATOR_HINT, inline=False)
    return _footer(embed, "⚜ Royal Court Casino · 50% win · 2× return · public table")

def allin_confirm(
    *,
    user_mention: str,
    number: int,
    stake: int,
    gross: int,
    chance: str,
    house: int,
) -> discord.Embed:
    """The public proposal before the all-in button is pressed."""
    embed = discord.Embed(
        title="🎰 Confirm Your All-In",
        description=(
            f"## {number}× or nothing\n"
            f"{user_mention} is pushing **their whole wallet** onto one draw.\n\n"
            "The table is watching. Press **Bet it all** only if you mean it."
        ),
        color=config.GOLD_COLOR,
    )
    embed.add_field(name="All-In Stake", value=money(stake), inline=True)
    embed.add_field(name="Payout If It Hits", value=money(gross), inline=True)
    embed.add_field(name="Chance", value=chance, inline=True)
    embed.add_field(name="Casino Stack", value=money(house), inline=True)
    embed.add_field(
        name="The Floor",
        value=f"{SPECTATOR_HINT}\n\n🔒 Only {user_mention} can press the button.",
        inline=False,
    )
    return _footer(embed, "⚜ Royal Court Casino · all-in is final · public table")


def allin_result(
    *,
    user_mention: str,
    number: int,
    stake: int,
    gross: int,
    balance: int,
    house: int,
    won: bool,
) -> discord.Embed:
    """The big all-in result card."""
    title = "🎰 You Win Big" if won else "🎰 You Lose It All"
    color = config.GOLD_COLOR if won else config.RED_COLOR
    net = gross - stake if won else -stake
    embed = discord.Embed(
        title=title,
        description=(
            f"## {'💥 HIT' if won else '💀 MISS'}\n"
            f"{user_mention} went all-in at **{number}×**. "
            + (
                f"The draw landed and the Casino pays **{money(gross)}**."
                if won
                else "The draw missed and the wallet is empty — the Casino takes the pot."
            )
        ),
        color=color,
    )
    embed.add_field(name="Stake", value=money(stake), inline=True)
    embed.add_field(name="Return", value=money(gross) if won else "—", inline=True)
    embed.add_field(name="Net", value=f"{net:+,}", inline=True)
    embed.add_field(name="Player Wallet", value=money(balance), inline=True)
    embed.add_field(name="Casino Stack", value=money(house), inline=True)
    embed.add_field(name="The Floor", value=SPECTATOR_HINT, inline=False)
    return _footer(embed, "⚜ Royal Court Casino · all-in is final · public table")


def _clip(text: str, limit: int = 1024) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _cards(cards) -> str:
    return " ".join(cards) if cards else "—"


def _poker_action(game) -> str:
    if game.complete:
        return "Hand complete."
    if game.actor == 0:
        return "**Your move.**"
    if game.actor is None:
        return "Dealing…"
    return f"**{game.seats[game.actor].name}** is thinking…"


def poker_table(*, user_mention: str, stake: int, game, footer: str) -> discord.Embed:
    """The public table before a result: only the player's cards are visible."""
    board = _cards(game.board) if game.board else "Pre-flop — no community cards yet"
    player = game.player()
    stacks = []
    for seat in game.seats:
        icon = "👤" if seat.is_player else "🤖"
        state = " · folded" if seat.folded else " · all-in" if seat.all_in else ""
        stacks.append(f"{icon} **{seat.name}** — {money(seat.stack)}{state}")
    log_lines = game.log[-4:]
    talk = "\n".join(f"• {line}" for line in log_lines) or "The table is quiet."
    embed = discord.Embed(
        title="♠️ TEXAS HOLD'EM · The High Table",
        description=(
            f"## {game.street.title()}\n"
            f"{user_mention} bought in for **{money(stake)}**.\n"
            f"👀 **The table is watching.** Only the player can act; "
            f"spectators can react 🎉 or 💀 after the result.\n\n"
            f"{_poker_action(game)}"
        ),
        color=config.GOLD_COLOR,
    )
    embed.add_field(name="The Board", value=_clip(board), inline=False)
    embed.add_field(name="Your Hand", value=_cards(player.hole), inline=True)
    embed.add_field(name="Pot", value=money(game.pot), inline=True)
    embed.add_field(
        name="To Call",
        value=money(game.to_call(player)) if game.to_call(player) else "Check is free",
        inline=True,
    )
    embed.add_field(name="Stacks", value=_clip("\n".join(stacks)), inline=False)
    embed.add_field(name="Table Talk", value=_clip(talk), inline=False)
    embed.set_footer(text=footer)
    return embed


def poker_result(
    *,
    user_mention: str,
    stake: int,
    final_stack: int,
    balance: int,
    house: int,
    game,
    outcome: str,
    footer: str,
) -> discord.Embed:
    """The closing card: reveal every hand and the final chip count."""
    if outcome == "win":
        title, color, headline = "♠️ POKER · You Win", config.GOLD_COLOR, "The pot is yours."
    elif outcome == "push":
        title, color, headline = "♠️ POKER · Push", discord.Color.blurple(), "The buy-in comes back."
    else:
        title, color, headline = "♠️ POKER · You Lose", config.RED_COLOR, "The house takes the pot."
    net = final_stack - stake
    winners = [
        f"{game.seats[index].name} ({money(amount)})"
        for index, amount in enumerate(game.winnings)
        if int(amount) > 0
    ]
    hands = "\n".join(
        f"{'👤' if seat.is_player else '🤖'} **{seat.name}** — {_cards(seat.hole)}"
        for seat in game.seats
    )
    embed = discord.Embed(
        title=title,
        description=(
            f"## {headline}\n"
            f"{user_mention} bought in for **{money(stake)}** and leaves with "
            f"**{money(final_stack)}**.\n\n"
            f"**Board:** {_cards(game.board) if game.board else '—'}"
        ),
        color=color,
    )
    embed.add_field(name="Returned Stack", value=money(final_stack), inline=True)
    embed.add_field(name="Net", value=f"{net:+,}", inline=True)
    embed.add_field(name="Wallet", value=money(balance), inline=True)
    embed.add_field(name="Casino Stack", value=money(house), inline=True)
    embed.add_field(
        name="Winners",
        value=_clip(", ".join(winners) or "No chips won"),
        inline=False,
    )
    embed.add_field(name="All Hands", value=_clip(hands), inline=False)
    embed.add_field(name="The Floor", value=SPECTATOR_HINT, inline=False)
    return _footer(embed, footer or "⚜ Royal Court Casino · Texas Hold'em · public table")


def casino_floor(house: int) -> discord.Embed:
    """The public house dashboard shown by /casino balance."""
    embed = discord.Embed(
        title="🎰 The Casino · House & Floor",
        description=(
            f"## {money(house)}\n"
            "The house is the counterparty for every wager. Lose and the house "
            "grows; win and it pays you from its own stack. The floor is public, "
            "so bring an audience."
        ),
        color=config.GOLD_COLOR,
    )
    embed.add_field(
        name="Tonight on the floor",
        value=(
            "🪙 **`/individual coinflip`** — call the coin · 50% win · 2×\n"
            "🎰 **`/allin`** — whole wallet, one draw · 2–100×\n"
            "🃏 **`/blackjack`** — hit or stand · natural pays 3:2\n"
            "♠️ **`/poker`** — four-seat Texas Hold'em · 200 minimum"
        ),
        inline=False,
    )
    embed.add_field(
        name="House rules",
        value=(
            "Every wager is settled against this stack; no bet can mint credits. "
            "A payout the house cannot cover is refused before the draw. "
            "The house is topped up by an administrator with `/casino fund`."
        ),
        inline=False,
    )
    embed.add_field(name="The Floor", value=SPECTATOR_HINT, inline=False)
    return _footer(embed)


async def add_spectator_reactions(message, *, good: bool) -> None:
    """Add reaction bait to a public result; silently skip fake/deleted messages."""
    if message is None:
        return
    for emoji in (("🎉", "🪙") if good else ("💀", "🪙")):
        try:
            await message.add_reaction(emoji)
        except Exception:
            # A missing permission, deleted message or test fake must never turn
            # a settled bet into an error.
            return

