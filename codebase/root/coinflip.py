"""
coinflip.py
===========
The ``/individual coinflip`` game, as pure arithmetic.

A player calls heads or tails and stakes credits. The player wins
``config.COINFLIP_WIN_BP`` times in 10,000 (5,000 by default, an even 50%) and
loses the remaining half, whichever face they called. Their call therefore
decides only which face is *shown* on a win, never the likelihood of winning.
At the default 50% win rate with a 2x return the game is a fair pass-through
(``house_edge_bp() == 0``); the Casino still funds each win from its own balance,
so variance can move the house stack but the long-run drift is zero. Lower
``COINFLIP_WIN_BP`` makes the game a sink; raising it above 5,000 makes it a
faucet. See :func:`house_edge_bp` before changing the rate or the multiplier.

Nothing here touches Discord or the database, so the odds can be checked without
a login: see ``checks/check_coinflip.py``.
"""

from __future__ import annotations

import random
from typing import Optional

import config

HEADS = "heads"
TAILS = "tails"
SIDES = (HEADS, TAILS)

BP_DENOMINATOR = 10_000


def flip_other(side: str) -> str:
    """The opposite face."""
    return TAILS if side == HEADS else HEADS


def resolve(choice: str, roll_bp: int) -> tuple[bool, str]:
    """Resolve one flip.

    ``roll_bp`` is an integer in ``[0, 10_000)``; ``choice`` is ``heads`` or
    ``tails``. Returns ``(won, landed_side)``. The player wins when the roll is
    below ``config.COINFLIP_WIN_BP``, so the win rate is exactly the configured
    one no matter which face was called; ``landed_side`` is the called face on a
    win and the other face on a loss, which keeps the story coherent.
    """
    if choice not in SIDES:
        raise ValueError(f"side must be one of {SIDES}, not {choice!r}")
    if not 0 <= roll_bp < BP_DENOMINATOR:
        raise ValueError(f"roll_bp must be in [0, {BP_DENOMINATOR})")
    won = roll_bp < config.COINFLIP_WIN_BP
    return won, choice if won else flip_other(choice)


def roll(rng: Optional[random.Random] = None) -> int:
    """A random basis point. Pass a seeded ``Random`` to make a test repeatable."""
    return (rng or random).randrange(BP_DENOMINATOR)


def gross_payout(stake: int) -> int:
    """Credits returned on a win: the stake plus equal winnings."""
    return int(stake) * config.COINFLIP_PAYOUT_MULTIPLIER


def net_change(stake: int, won: bool) -> int:
    """Net effect of one settled bet on the wallet, for the ledger row."""
    return int(stake) if won else -int(stake)


def stake_problem(stake: int, balance: int) -> Optional[str]:
    """Why this stake cannot be placed, or ``None`` when it can.

    Returns a user-facing sentence. The cap check matters: a win credits the
    wallet, so a stake that would push the balance past
    ``config.INDIVIDUAL_MAX_CREDITS`` has to be refused *before* the flip rather
    than rolling back a legitimate win.
    """
    if stake < config.COINFLIP_MIN_STAKE:
        return f"The minimum stake is **{config.COINFLIP_MIN_STAKE:,}** credits."
    if stake > balance:
        return "You do not hold that much in your individual balance."
    if stake > config.COINFLIP_MAX_STAKE:
        return f"The maximum stake is **{config.COINFLIP_MAX_STAKE:,}** credits."
    room = config.INDIVIDUAL_MAX_CREDITS - balance
    if config.COINFLIP_PAYOUT_MULTIPLIER > 1 and stake > room:
        return (
            "A win would take you past the individual cap of "
            f"**{config.INDIVIDUAL_MAX_CREDITS:,}** credits. Stake less."
        )
    return None


def house_edge_bp() -> int:
    """Long-run credits destroyed per 10,000 staked.

    ``10_000 - win_bp * payout_multiplier``: positive means the game is a sink,
    zero means it is zero-sum, negative means it prints money.
    """
    return BP_DENOMINATOR - config.COINFLIP_WIN_BP * config.COINFLIP_PAYOUT_MULTIPLIER


def win_label() -> str:
    """Win rate as a percentage string, e.g. 5000 bp -> '50'."""
    whole, frac = divmod(config.COINFLIP_WIN_BP, 100)
    return str(whole) if frac == 0 else f"{whole}.{frac:02d}".rstrip("0")
