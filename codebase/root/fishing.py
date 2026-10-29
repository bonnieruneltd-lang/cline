"""
fishing.py
==========
The ``/fish`` catch, as pure arithmetic.

A catch is one random fish from ``config.FISHING_NAMES`` worth a random number
of credits between ``config.FISHING_MIN_CREDITS`` and
``config.FISHING_MAX_CREDITS`` (20–100 by default). The credits are paid by the
Fishing Guild house account, not created: the cog debits
``fishing_guild_account`` before it credits the player's individual wallet.

Nothing here touches Discord or the database, so the range and the refusal
rules can be checked without a login: see ``checks/check_fishing.py``.
"""

from __future__ import annotations

import random
from typing import Optional, Tuple

import config


def catch(rng: Optional[random.Random] = None) -> Tuple[str, int]:
    """Return ``(fish_name, credit_value)`` for one catch.

    The value is inclusive of both bounds. Pass a seeded ``random.Random`` to
    make a test repeatable.
    """
    chooser = rng or random
    name = chooser.choice(config.FISHING_NAMES)
    payout = chooser.randint(config.FISHING_MIN_CREDITS, config.FISHING_MAX_CREDITS)
    return str(name), int(payout)


def catch_problem(
    *,
    payout: int,
    balance: int,
    house: int,
    cap: Optional[int] = None,
) -> Optional[str]:
    """Why this catch cannot be paid, or ``None`` when it can.

    The player is credited, so the 100M individual cap has to be checked before
    the Fishing Guild is debited. The house must also hold the whole payout;
    otherwise the guild would owe credits it does not have.
    """
    payout = int(payout)
    balance = int(balance)
    house = int(house)
    cap = config.INDIVIDUAL_MAX_CREDITS if cap is None else int(cap)

    if payout < config.FISHING_MIN_CREDITS or payout > config.FISHING_MAX_CREDITS:
        return (
            f"The Fishing Guild only pays for fish worth "
            f"**{config.FISHING_MIN_CREDITS:,}–{config.FISHING_MAX_CREDITS:,}** credits."
        )
    if balance + payout > cap:
        return (
            f"A **{payout:,}** credit catch would take you past the individual cap of "
            f"**{cap:,}** credits. Spend some credits before fishing again."
        )
    if house < payout:
        return (
            f"The Fishing Guild holds **{house:,}** credits and cannot pay a "
            f"**{payout:,}** credit catch. Ask an admin to fund it with "
            f"`/fishingguild fund`."
        )
    return None
