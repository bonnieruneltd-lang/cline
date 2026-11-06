"""
slots.py
========
The ``/slots`` machine, as pure arithmetic.

Three reels spin independently over the same weighted strip of symbols. A spin
pays only on **three of a kind**, and the multiple depends on the symbol: the
common cherry pays little, the single seven pays the jackpot. Two of a kind is
exactly the near miss it looks like - it pays nothing.

Because every reel uses the same strip, the whole game can be priced in closed
form. Three of a kind on one symbol has probability ``(weight / TOTAL)^3``, so

    expected return = sum(weight^3 * payout) / TOTAL^3

With the shipped table that is ``58,382 / 64,000`` = 0.9122: an **8.78% house
edge**, and 3.95% of spins pay. The machine is therefore a sink like ``/allin``
(9.09%) rather than a faucet like coinflip (0%), and the Casino still has to
*cover* the jackpot before a spin is allowed - :func:`max_gross` is what
``casino.wager_problem()`` reserves, and it is the top payout, not the average.

Nothing here touches Discord or the database, so every number below can be
checked without a login: see ``checks/check_slots.py``, which prices all
``TOTAL_WEIGHT ** 3`` reel outcomes by brute force and compares.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from fractions import Fraction
from typing import Optional, Sequence, Tuple

import config

REELS = 3


@dataclass(frozen=True)
class Symbol:
    """One face on the strip: how often it lands and what three of it pays."""

    emoji: str
    name: str
    weight: int                    # rungs on the strip for one reel
    payout: int                    # gross multiple of the stake on three of a kind

    @property
    def label(self) -> str:
        return f"{self.emoji} {self.name}"

    @property
    def plural(self) -> str:
        """``Cherries``, ``Sevens`` - for the result line."""
        return f"{self.name[:-1]}ies" if self.name.endswith("y") else f"{self.name}s"


def _build() -> Tuple[Symbol, ...]:
    symbols = tuple(
        Symbol(str(emoji), str(name), int(weight), int(payout))
        for emoji, name, weight, payout in config.SLOTS_SYMBOLS
    )
    if len(symbols) < 2:
        raise ValueError("config.SLOTS_SYMBOLS needs at least two symbols")
    for symbol in symbols:
        if symbol.weight < 1:
            raise ValueError(f"{symbol.name} needs a weight of at least 1")
        if symbol.payout < 1:
            raise ValueError(f"{symbol.name} needs a payout of at least 1")
    return symbols


SYMBOLS: Tuple[Symbol, ...] = _build()

# One entry per rung of the strip, holding the index of the symbol on it. A reel
# is a uniform draw from this tuple, which is what makes the weights literal.
STRIP: Tuple[int, ...] = tuple(
    index for index, symbol in enumerate(SYMBOLS) for _ in range(symbol.weight)
)
TOTAL_WEIGHT = len(STRIP)
MAX_PAYOUT = max(symbol.payout for symbol in SYMBOLS)
MAX_PAYOUT_SYMBOL = max(SYMBOLS, key=lambda symbol: symbol.payout)


# ---------------------------------------------------------------------------
# The spin
# ---------------------------------------------------------------------------
def spin(rng: Optional[random.Random] = None) -> Tuple[Symbol, ...]:
    """Spin all three reels. Pass a seeded ``Random`` for a repeatable test."""
    picker = rng or random
    return tuple(SYMBOLS[STRIP[picker.randrange(TOTAL_WEIGHT)]] for _ in range(REELS))


def multiplier(reels: Sequence[Symbol]) -> int:
    """Gross multiple paid by this spin: ``0`` unless all three reels match."""
    if len(reels) != REELS:
        raise ValueError(f"a spin has exactly {REELS} reels")
    first = reels[0]
    return first.payout if all(reel == first for reel in reels) else 0


def won(reels: Sequence[Symbol]) -> bool:
    """Whether this spin pays anything."""
    return multiplier(reels) > 0


def gross_payout(reels: Sequence[Symbol], stake: int) -> int:
    """Credits returned by this spin: the multiple times the stake, or nothing."""
    return multiplier(reels) * int(stake)


def max_gross(stake: int) -> int:
    """Worst case the house must cover before a spin is allowed."""
    return MAX_PAYOUT * int(stake)


def net_change(reels: Sequence[Symbol], stake: int) -> int:
    """Net effect of the settled spin on the wallet, for display and tests."""
    stake = int(stake)
    multiple = multiplier(reels)
    return stake * (multiple - 1) if multiple else -stake


def label(reels: Sequence[Symbol]) -> str:
    """The machine line: ``🍒 │ 🍒 │ 🍒``."""
    return " │ ".join(reel.emoji for reel in reels)


def names(reels: Sequence[Symbol]) -> str:
    """Spelled-out result, e.g. ``three Cherries`` or ``no match``."""
    if won(reels):
        return f"three {reels[0].plural}"
    return "no match"


def partial(reels: Sequence[Symbol]) -> Optional[str]:
    """The near miss, if this losing spin had a pair: ``two Lemons``."""
    if won(reels):
        return None
    for symbol in reels:
        if sum(1 for reel in reels if reel == symbol) == 2:
            return f"two {symbol.plural}"
    return None


# ---------------------------------------------------------------------------
# Exact odds (the whole paytable, priced)
# ---------------------------------------------------------------------------
def expected_return() -> Fraction:
    """Exact share of every staked credit the machine returns, as a fraction."""
    paid = sum(symbol.weight ** REELS * symbol.payout for symbol in SYMBOLS)
    return Fraction(paid, TOTAL_WEIGHT ** REELS)


def house_edge() -> Fraction:
    """Long-run share the house keeps: ``1 - expected_return()``."""
    return 1 - expected_return()


def win_chance() -> Fraction:
    """Probability that a spin pays anything at all."""
    return Fraction(
        sum(symbol.weight ** REELS for symbol in SYMBOLS), TOTAL_WEIGHT ** REELS
    )


def chance_of(symbol: Symbol) -> Fraction:
    """Probability of three of one specific symbol."""
    return Fraction(symbol.weight ** REELS, TOTAL_WEIGHT ** REELS)


def _percent(value: Fraction, places: int = 2) -> str:
    return f"{float(value) * 100:.{places}f}%"


def edge_label() -> str:
    """``8.78%`` - the house edge, for the embed footer."""
    return _percent(house_edge())


def return_label() -> str:
    """``91.22%`` - the return to the player, for the embed footer."""
    return _percent(expected_return())


def chance_label() -> str:
    """``1 in 25.3`` - how rare a paying spin is."""
    odds = 1 / win_chance()
    return f"1 in {odds.numerator}" if odds.denominator == 1 else f"1 in {float(odds):.3g}"


def paytable() -> str:
    """The paytable as one line per symbol, highest payer first."""
    rows = []
    for symbol in sorted(SYMBOLS, key=lambda symbol: -symbol.payout):
        chance = 1 / chance_of(symbol)
        odds = (
            f"1 in {chance.numerator:,}"
            if chance.denominator == 1
            else f"1 in {float(chance):,.0f}"
        )
        rows.append(f"{symbol.label} · **{symbol.payout}×** · {odds}")
    return "\n".join(rows)


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------
def stake_problem(stake: int, balance: int) -> Optional[str]:
    """Why this stake cannot be spun, or ``None`` when it can.

    The cap is checked against the *jackpot*, not the stake: a 250x symbol can
    land on any spin, so a stake whose best case would cross
    ``config.INDIVIDUAL_MAX_CREDITS`` has to be refused before the reels turn
    rather than rolled back after a legitimate win.
    """
    stake = int(stake)
    balance = int(balance)
    if stake < config.SLOTS_MIN_STAKE:
        return f"The minimum spin is **{config.SLOTS_MIN_STAKE:,}** credits."
    if stake > config.SLOTS_MAX_STAKE:
        return f"The maximum spin is **{config.SLOTS_MAX_STAKE:,}** credits."
    if stake > balance:
        return "You do not hold that much in your individual balance."
    if balance - stake + max_gross(stake) > config.INDIVIDUAL_MAX_CREDITS:
        return (
            f"A **{MAX_PAYOUT}×** jackpot on that stake would pay "
            f"{max_gross(stake):,} and take you past the individual cap of "
            f"**{config.INDIVIDUAL_MAX_CREDITS:,}** credits. Spin less."
        )
    return None
