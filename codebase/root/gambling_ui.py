"""
gambling_ui.py
==============
Shared spectator-facing presentation for the public gambling tables.

Every result is posted in the channel, so the card has to read at a glance: one
headline with the outcome, three figures (bet, payout, wallet) and one line
inviting the audience to react. The house stack is not repeated on every hand —
it lives on ``/casino balance``. No money logic lives here. The same module
renders the public Texas Hold'em table and its showdown card.
"""
from __future__ import annotations

import discord

import config
from utils import money

SPECTATOR_HINT = (
    "👀 **The table is watching** — react 🎉 to cheer a win or 💀 for a bust."
)
CASINO_FOOTER = "⚜ Royal Court Casino · every wager is public"


def _footer(embed: discord.Embed, text: str = CASINO_FOOTER) -> discord.Embed:
    embed.set_footer(text=text)
    return embed


def signed_money(amount: int) -> str:
    """``+1,000 credits`` / ``-1,000 credits`` for a net line."""
    return f"{'+' if amount >= 0 else '-'}{money(abs(int(amount)))}"


def _figures(
    embed: discord.Embed, *, stake: int, gross: int, balance: int, won: bool
) -> discord.Embed:
    """The three figures every result card shows: bet, payout and wallet.

    One row, three fields, net folded into the payout so the card stays short.
    The house stack is deliberately left to ``/casino balance`` instead of being
    repeated on every hand.
    """
    net = gross - stake if won else -stake
    payout = (
        f"**{money(gross)}**\nnet {signed_money(net)}"
        if won
        else f"—\nnet {signed_money(net)}"
    )
    embed.add_field(name="Bet", value=money(stake), inline=True)
    embed.add_field(name="Payout", value=payout, inline=True)
    embed.add_field(name="Wallet", value=money(balance), inline=True)
    embed.add_field(name="The Floor", value=SPECTATOR_HINT, inline=False)
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
    """A one-glance coinflip result for the channel."""
    title = "🪙 COINFLIP · You Win" if won else "🪙 COINFLIP · You Lose"
    color = config.GOLD_COLOR if won else config.RED_COLOR
    payout = (
        f"the Casino pays **{money(gross)}**"
        if won
        else "the stake goes to the house"
    )
    embed = discord.Embed(
        title=title,
        description=(
            f"## 🪙 {landed.title()}\n"
            f"{user_mention} called **{called}** · landed **{landed}** — {payout}."
        ),
        color=color,
    )
    _figures(embed, stake=stake, gross=gross, balance=balance, won=won)
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
            f"{user_mention} is putting **their whole wallet** on one draw.\n\n"
            "Press **Bet it all** only if you mean it."
        ),
        color=config.GOLD_COLOR,
    )
    embed.add_field(name="All-In", value=money(stake), inline=True)
    embed.add_field(name="Pays If It Hits", value=money(gross), inline=True)
    embed.add_field(name="Odds", value=chance, inline=True)
    embed.add_field(
        name="The Floor",
        value=f"{SPECTATOR_HINT}\n🔒 Only {user_mention} can press the button.",
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
    embed = discord.Embed(
        title=title,
        description=(
            f"## {'💥 HIT' if won else '💀 MISS'} at {number}×\n"
            + (
                f"{user_mention} hit the draw — the Casino pays **{money(gross)}**."
                if won
                else f"{user_mention} missed — the whole wallet goes to the house."
            )
        ),
        color=color,
    )
    _figures(embed, stake=stake, gross=gross, balance=balance, won=won)
    return _footer(embed, "⚜ Royal Court Casino · all-in is final · public table")


def slots_result(
    *,
    user_mention: str,
    machine: str,
    result: str,
    near_miss: str | None,
    stake: int,
    gross: int,
    balance: int,
    house: int,
    won: bool,
    multiple: int,
    chance: str,
    edge: str,
) -> discord.Embed:
    """The three-reel card. ``machine`` is the reel line, e.g. ``🍒 │ 🍋 │ 🍒``."""
    title = "🎰 SLOTS · You Win" if won else "🎰 SLOTS · No Match"
    color = config.GOLD_COLOR if won else config.RED_COLOR
    if won:
        story = (
            f"{user_mention} lined up **{result}** — **{multiple}×** — and the "
            f"Casino pays **{money(gross)}**."
        )
    elif near_miss:
        story = (
            f"{user_mention} hit **{near_miss}** — so close, but the reels have to "
            "match three deep. The stake goes to the house."
        )
    else:
        story = f"{user_mention} came up with {result}. The stake goes to the house."
    embed = discord.Embed(
        title=title,
        description=f"## {machine}\n{story}",
        color=color,
    )
    _figures(embed, stake=stake, gross=gross, balance=balance, won=won)
    return _footer(
        embed,
        f"⚜ Royal Court Casino · three of a kind pays · {chance} odds · "
        f"house keeps {edge}",
    )


def roulette_result(
    *,
    user_mention: str,
    bet_label: str,
    odds_label: str,
    pocket: int,
    colour: str,
    stake: int,
    gross: int,
    balance: int,
    house: int,
    won: bool,
) -> discord.Embed:
    """The wheel card. ``bet_label`` and ``odds_label`` describe the bet placed."""
    title = "🎡 ROULETTE · You Win" if won else "🎡 ROULETTE · You Lose"
    color = config.GOLD_COLOR if won else config.RED_COLOR
    zero = (
        "\nThe green zero takes every outside bet — that is the house edge."
        if colour == "green"
        else ""
    )
    story = (
        f"{user_mention} backed **{bet_label}** at {odds_label} · ball landed on "
        f"**{pocket} ({colour})**.\n"
        + (
            f"The Casino pays **{money(gross)}**."
            if won
            else "The stake goes to the house."
        )
        + zero
    )
    embed = discord.Embed(
        title=title,
        description=f"## {pocket} · {colour}\n{story}",
        color=color,
    )
    _figures(embed, stake=stake, gross=gross, balance=balance, won=won)
    return _footer(
        embed, "⚜ Royal Court Casino · single-zero wheel · 2.70% house edge · public table"
    )


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
    log_lines = game.log[-3:]
    talk = "\n".join(f"• {line}" for line in log_lines) or "The table is quiet."
    heading = "Showdown" if game.complete else game.street.title()
    to_call = game.to_call(player)
    if game.complete:
        call_value = "—"
    elif to_call:
        call_value = money(to_call)
    else:
        call_value = "Check is free"
    embed = discord.Embed(
        title="♠️ TEXAS HOLD'EM · The High Table",
        description=(
            f"## {heading} · pot {money(game.pot)}\n"
            f"{user_mention} bought in for **{money(stake)}** · 👀 table is watching\n\n"
            f"{_poker_action(game)}"
        ),
        color=config.GOLD_COLOR,
    )
    embed.add_field(name="Board", value=_clip(board), inline=False)
    embed.add_field(name="Your Hand", value=_cards(player.hole), inline=True)
    embed.add_field(name="To Call", value=call_value, inline=True)
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
    embed.add_field(name="Returned", value=money(final_stack), inline=True)
    embed.add_field(name="Net", value=signed_money(net), inline=True)
    embed.add_field(name="Wallet", value=money(balance), inline=True)
    embed.add_field(
        name="Winners",
        value=_clip(", ".join(winners) or "No chips won"),
        inline=False,
    )
    embed.add_field(name="All Hands", value=_clip(hands), inline=False)
    embed.add_field(name="The Floor", value=SPECTATOR_HINT, inline=False)
    return _footer(embed, footer or "⚜ Royal Court Casino · Texas Hold'em · public table")


def casino_floor(house: int, reserved: int = 0, reserved_note: str = "") -> discord.Embed:
    """The public house dashboard shown by /casino balance.

    ``reserved`` is the worst-case payout still owed on the hands in play
    (``casino.reserve_for_open_hands``). It is shown as a separate line so the
    public balance keeps meaning "credits the house holds", while admins can see
    what part of it is already spoken for.
    """
    embed = discord.Embed(
        title="🎰 The Casino · House & Floor",
        description=(
            f"## {money(house)}\n"
            "The house pays every win and keeps every loss. It can never pay out "
            "more than it holds."
        ),
        color=config.GOLD_COLOR,
    )
    embed.add_field(
        name="Tonight on the floor",
        value=(
            "🪙 **`/individual coinflip`** · 50% win · 2×\n"
            "🎰 **`/allin`** · whole wallet · 2–100×\n"
            "🍒 **`/slots`** · three reels · up to 250×\n"
            "🎡 **`/roulette`** · single zero · 35:1\n"
            "🃏 **`/blackjack`** · hit or stand · 3:2\n"
            "♠️ **`/poker`** · four seats · 200 minimum"
        ),
        inline=False,
    )
    embed.add_field(
        name="House rules",
        value=(
            "No bet can mint credits. A payout the house cannot cover is refused "
            "before the draw. Admins top the house up with `/casino fund`."
        ),
        inline=False,
    )
    if int(reserved) > 0:
        held = f" — {reserved_note}" if reserved_note else ""
        embed.add_field(
            name="Held for open hands",
            value=(
                f"**{money(int(reserved))}** stays behind for the hands still in play"
                f"{held}. `/casino withdraw` cannot touch it."
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

