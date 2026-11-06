"""
roulette.py
===========
The ``/roulette`` wheel, as pure arithmetic.

A **European single-zero** wheel: one green zero and the numbers 1-36, eighteen
red and eighteen black. The standard bets are offered - straight up, red/black,
odd/even, low/high, the three dozens and the three columns - with the standard
payouts:

    straight up   35:1   gross 36x        dozen / column   2:1   gross 3x
    even money     1:1   gross  2x        zero             loses every one

On a single-zero wheel every one of those prices identically:

    18/37 * 2 = 36/37      12/37 * 3 = 36/37      1/37 * 36 = 36/37

so the house edge is a flat **2.70%** whichever bet is placed - unlike the US
double-zero wheel, which returns 36/38 (5.26%). Covering both the zero and the
choices is what makes that work: zero is not red, not black, not odd, not even,
not low, not high, and sits in no column and no dozen.

Nothing here touches Discord or the database: :func:`resolve` decides a bet from
a pocket number alone, so ``checks/check_roulette.py`` walks all 37 pockets for
every bet shape and prices each one exactly.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from fractions import Fraction
from typing import Optional, Tuple

import config

ZERO = 0
GREEN = "green"
RED = "red"
BLACK = "black"

# The canonical European layout. Red and black partition 1-36; zero is green and
# belongs to neither, which is where the house edge comes from.
RED_NUMBERS = frozenset(
    {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}
)
BLACK_NUMBERS = frozenset(range(1, 37)) - RED_NUMBERS
NUMBERS = frozenset(range(1, 37))

DOZENS = {
    "1st12": frozenset(range(1, 13)),
    "2nd12": frozenset(range(13, 25)),
    "3rd12": frozenset(range(25, 37)),
}
COLUMNS = {
    "column1": frozenset(range(1, 37, 3)),      # 1, 4, 7 ... 34
    "column2": frozenset(range(2, 37, 3)),      # 2, 5, 8 ... 35
    "column3": frozenset(range(3, 37, 3)),      # 3, 6, 9 ... 36
}
EVEN_MONEY = {
    "red": RED_NUMBERS,
    "black": BLACK_NUMBERS,
    "odd": frozenset(number for number in NUMBERS if number % 2 == 1),
    "even": frozenset(number for number in NUMBERS if number % 2 == 0),
    "low": frozenset(range(1, 19)),
    "high": frozenset(range(19, 37)),
}

STRAIGHT = "straight"
BET_KINDS: Tuple[str, ...] = (
    "red", "black", "odd", "even", "low", "high",
    "1st12", "2nd12", "3rd12", "column1", "column2", "column3", STRAIGHT,
)

# Discord presents these as the command's choices: (label, value).
CHOICES: Tuple[Tuple[str, str], ...] = (
    ("Red", "red"),
    ("Black", "black"),
    ("Odd", "odd"),
    ("Even", "even"),
    ("Low (1-18)", "low"),
    ("High (19-36)", "high"),
    ("1st dozen (1-12)", "1st12"),
    ("2nd dozen (13-24)", "2nd12"),
    ("3rd dozen (25-36)", "3rd12"),
    ("1st column", "column1"),
    ("2nd column", "column2"),
    ("3rd column", "column3"),
    ("Straight up (pick a number)", STRAIGHT),
)

_NAMES = {
    "red": "Red",
    "black": "Black",
    "odd": "Odd",
    "even": "Even",
    "low": "Low (1-18)",
    "high": "High (19-36)",
    "1st12": "1st dozen",
    "2nd12": "2nd dozen",
    "3rd12": "3rd dozen",
    "column1": "1st column",
    "column2": "2nd column",
    "column3": "3rd column",
}


@dataclass(frozen=True)
class Bet:
    """One settled bet shape: what it covers and what it returns."""

    kind: str                       # "red", "1st12", "column2", "straight", ...
    selection: Optional[int]        # the number for a straight bet, else None
    numbers: frozenset              # every pocket that pays this bet
    multiple: int                   # gross return on a win, stake included

    @property
    def label(self) -> str:
        if self.kind == STRAIGHT:
            return f"Straight up {self.selection}"
        return _NAMES[self.kind]

    @property
    def coverage(self) -> str:
        """How much of the wheel this bet covers: ``18 of 37 pockets``."""
        return f"{len(self.numbers)} of {config.ROULETTE_POCKETS} pockets"

    @property
    def odds_label(self) -> str:
        """``35:1``, ``1:1``, ``2:1`` - the profit, not the gross return."""
        return f"{self.multiple - 1}:1"


def make_bet(kind: str, selection: Optional[int] = None) -> Bet:
    """Build the bet for a choice, or raise ``ValueError`` if it is not offered."""
    if kind not in BET_KINDS:
        raise ValueError(f"{kind!r} is not a bet this wheel offers")
    if kind == STRAIGHT:
        if selection is None:
            raise ValueError("a straight-up bet needs a number from 0 to 36")
        number = int(selection)
        if not ZERO <= number <= 36:
            raise ValueError("a straight-up bet takes a number from 0 to 36")
        return Bet(kind, number, frozenset({number}), config.ROULETTE_STRAIGHT_MULTIPLE)
    if selection is not None:
        raise ValueError(f"only a straight-up bet takes a number, not {kind!r}")
    if kind in EVEN_MONEY:
        return Bet(kind, None, EVEN_MONEY[kind], config.ROULETTE_EVEN_MONEY_MULTIPLE)
    if kind in DOZENS:
        return Bet(kind, None, DOZENS[kind], config.ROULETTE_DOZEN_MULTIPLE)
    return Bet(kind, None, COLUMNS[kind], config.ROULETTE_COLUMN_MULTIPLE)


# ---------------------------------------------------------------------------
# The wheel
# ---------------------------------------------------------------------------
def spin(rng: Optional[random.Random] = None) -> int:
    """Drop the ball: a pocket from 0 to 36. Pass a seeded ``Random`` for tests."""
    return (rng or random).randrange(config.ROULETTE_POCKETS)


def resolve(bet: Bet, pocket: int) -> bool:
    """Whether ``pocket`` pays ``bet``."""
    pocket = int(pocket)
    if not ZERO <= pocket <= 36:
        raise ValueError(f"a pocket is 0-36, not {pocket!r}")
    return pocket in bet.numbers


def colour(pocket: int) -> str:
    """``red``, ``black`` or ``green``."""
    pocket = int(pocket)
    if pocket == ZERO:
        return GREEN
    return RED if pocket in RED_NUMBERS else BLACK


def pocket_label(pocket: int) -> str:
    """``17 · black``, or ``0 · green``."""
    return f"{int(pocket)} · {colour(pocket)}"


def gross_payout(bet: Bet, stake: int, won: bool) -> int:
    """Credits returned: the multiple times the stake on a win, else nothing."""
    return bet.multiple * int(stake) if won else 0


def max_gross(stake: int) -> int:
    """Worst case the house must cover before a bet: always the straight up."""
    return config.ROULETTE_STRAIGHT_MULTIPLE * int(stake)


def net_change(bet: Bet, stake: int, won: bool) -> int:
    """Net effect of the settled bet on the wallet, for display and tests."""
    stake = int(stake)
    return stake * (bet.multiple - 1) if won else -stake


# ---------------------------------------------------------------------------
# Exact odds
# ---------------------------------------------------------------------------
def expected_return(bet: Bet) -> Fraction:
    """Exact share of every staked credit this bet returns."""
    return Fraction(len(bet.numbers), config.ROULETTE_POCKETS) * bet.multiple


def house_edge(bet: Bet) -> Fraction:
    """Long-run share the house keeps on this bet: ``1 - expected_return``."""
    return 1 - expected_return(bet)


def edge_label(bet: Bet) -> str:
    """``2.70%`` - the house edge on this bet."""
    return f"{float(house_edge(bet)) * 100:.2f}%"


def return_label(bet: Bet) -> str:
    """``97.30%`` - the return to the player on this bet."""
    return f"{float(expected_return(bet)) * 100:.2f}%"


def win_chance(bet: Bet) -> Fraction:
    """Probability this bet wins: its coverage over 37 pockets."""
    return Fraction(len(bet.numbers), config.ROULETTE_POCKETS)


def bet_table() -> str:
    """The bet menu, for the ``/roulette`` card."""
    rows = []
    for label, value in CHOICES:
        bet = make_bet(value, 0 if value == STRAIGHT else None)
        rows.append(f"{label} · {bet.odds_label} · {bet.coverage}")
    return "\n".join(rows)


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------
def stake_problem(bet: Bet, stake: int, balance: int) -> Optional[str]:
    """Why this bet cannot be placed, or ``None`` when it can.

    The cap is checked on the *bet's own* payout: a straight up returns 36x and
    can cross ``config.INDIVIDUAL_MAX_CREDITS`` from a modest stake, while an
    even-money bet of the same size cannot. Screening it here means a legitimate
    win is never rolled back afterwards.
    """
    stake = int(stake)
    balance = int(balance)
    if stake < config.ROULETTE_MIN_STAKE:
        return f"The minimum bet is **{config.ROULETTE_MIN_STAKE:,}** credits."
    if stake > config.ROULETTE_MAX_STAKE:
        return f"The maximum bet is **{config.ROULETTE_MAX_STAKE:,}** credits."
    if stake > balance:
        return "You do not hold that much in your individual balance."
    gross = bet.multiple * stake
    if balance - stake + gross > config.INDIVIDUAL_MAX_CREDITS:
        return (
            f"That **{bet.odds_label}** bet pays {gross:,} and would take you past "
            f"the individual cap of **{config.INDIVIDUAL_MAX_CREDITS:,}** credits. "
            "Bet less."
        )
    return None
