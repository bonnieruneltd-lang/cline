"""
allin.py
========
The ``/allin`` game, as pure arithmetic.

A player names a number ``n`` between ``config.ALLIN_MIN_NUMBER`` and
``config.ALLIN_MAX_NUMBER``, then stakes their whole individual wallet on a
single draw. Winning pays ``n`` times the stake; the chance of winning is
``1 in (hardness * n)``, where hardness is ``config.ALLIN_HARDNESS`` (1.1 by
default). So ``n = 5`` pays 5x at 1 in 5.5.

Why the "1.1x harder" rule matters: the expected return is

    n * 1/(h*n) = 1/h

which is *independent of n*. Every number carries the same house edge
(``1 - 1/1.1`` = 9.09% at the default), so picking 100 is not a worse bet than
picking 5 in expectation - it is only a higher-variance one. Change the hardness
and you change the edge for every number at once; see :func:`house_edge`.

The draw is kept in integer arithmetic: chance ``1/(h*n)`` is exactly
``DENOMINATOR / (NUMERATOR * n)``, so a draw is a uniform integer in
``[0, NUMERATOR * n)`` that wins when it lands below ``DENOMINATOR``. Nothing
here touches Discord or the database, so the odds can be checked without a
login: see ``checks/check_allin.py``.
"""

from __future__ import annotations

import random
from fractions import Fraction
from typing import Optional

import config


def _hardness() -> Fraction:
    return Fraction(config.ALLIN_HARDNESS_NUMERATOR, config.ALLIN_HARDNESS_DENOMINATOR)


def number_problem(number: int) -> Optional[str]:
    """Why this number cannot be played, or ``None`` when it can."""
    if number < config.ALLIN_MIN_NUMBER:
        # n = 1 pays 1x, i.e. the stake back with no profit, while still losing
        # the whole wallet on a miss. It is a trap, so it is not offered.
        return (
            f"Pick a number from **{config.ALLIN_MIN_NUMBER}** to "
            f"**{config.ALLIN_MAX_NUMBER}**. A 1x payout would only return your "
            "stake, so it is not worth the risk."
        )
    if number > config.ALLIN_MAX_NUMBER:
        return f"Pick a number from **{config.ALLIN_MIN_NUMBER}** to **{config.ALLIN_MAX_NUMBER}**."
    return None


def draw_size(number: int) -> int:
    """How many equally likely outcomes the draw has: ``NUMERATOR * n``."""
    return config.ALLIN_HARDNESS_NUMERATOR * int(number)


def winning_slips() -> int:
    """How many of those outcomes win: ``DENOMINATOR``, independent of ``n``."""
    return config.ALLIN_HARDNESS_DENOMINATOR


def resolve(number: int, roll: int) -> bool:
    """Decide one draw. ``roll`` must be in ``[0, draw_size(number))``."""
    if number_problem(number):
        raise ValueError(f"{number!r} is not a playable number")
    if not 0 <= roll < draw_size(number):
        raise ValueError(f"roll must be in [0, {draw_size(number)})")
    return roll < winning_slips()


def roll(number: int, rng: Optional[random.Random] = None) -> int:
    """A draw. Pass a seeded ``Random`` to make a test repeatable."""
    return (rng or random).randrange(draw_size(number))


def win_chance(number: int) -> Fraction:
    """Exact probability of winning, for display and for tests."""
    return Fraction(winning_slips(), draw_size(number))


def chance_label(number: int) -> str:
    """``1 in 5.5`` for n=5, ``1 in 7.7`` for n=7."""
    odds = _hardness() * int(number)
    if odds.denominator == 1:
        return f"1 in {odds.numerator}"
    return f"1 in {float(odds):.4g}"


def gross_payout(number: int, stake: int) -> int:
    """Credits returned on a win: the stake times ``n``."""
    return int(stake) * int(number)


def net_change(number: int, stake: int, won: bool) -> int:
    """Net effect of one settled bet on the wallet, for the ledger row."""
    stake = int(stake)
    return stake * (int(number) - 1) if won else -stake


def house_edge() -> Fraction:
    """Long-run share of every staked credit the house keeps: ``1 - 1/hardness``."""
    return 1 - 1 / _hardness()


def stake_problem(number: int, stake: int, balance: int) -> Optional[str]:
    """Why this stake cannot be placed, or ``None`` when it can.

    A win leaves the wallet at ``balance - stake + n * stake``, so the cap check
    is on the *payout*, not on the stake. Screening it here means a legitimate
    win is never rolled back afterwards.
    """
    if stake < config.ALLIN_MIN_BALANCE:
        return (
            f"Your wallet holds less than the **{config.ALLIN_MIN_BALANCE:,}** credit "
            "minimum for an all-in."
        )
    if stake > balance:
        return "You do not hold that much in your individual balance."
    final = balance - int(stake) + gross_payout(number, stake)
    if final > config.INDIVIDUAL_MAX_CREDITS:
        return (
            f"A **{number}x** win would pay {gross_payout(number, stake):,} and take you past "
            f"the individual cap of **{config.INDIVIDUAL_MAX_CREDITS:,}** credits. "
            "Pick a smaller number or stake less."
        )
    return None
