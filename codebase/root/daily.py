"""
daily.py
========
Pure streak and multiplier maths for ``/daily``. No Discord or database imports
so the rules can be tested in isolation, like ``payouts.py``.

Rules (fixed in ``config.py``)
------------------------------
* Base allowance is ``DAILY_BASE`` (2,000 credits) at a 100% multiplier.
* One claim per 24 hours. A claim made in the next 24-hour window after that
  (i.e. between 24 and 48 hours since the previous one) counts as consecutive
  and adds ``DAILY_BP_STEP`` (+10 percentage points).
* Each full 24-hour window missed beyond that subtracts
  ``DAILY_BP_MISS_PENALTY`` (-20 points), never below ``DAILY_BP_FLOOR`` (60%).
* The multiplier never exceeds ``DAILY_BP_CAP`` (400%, i.e. 8,000 credits).

The multiplier is stored in basis points (10,000 = 100%) so every payout is
integer maths, consistent with the rest of the economy.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from typing import Optional

import config

SECONDS_PER_DAY = 86_400


@dataclass(frozen=True)
class DailyOutcome:
    """What a claim at this moment would do."""

    eligible: bool
    multiplier_bp: int
    streak: int
    payout: int
    missed_days: int = 0
    retry_in: int = 0  # seconds until the next claim, when not eligible


def clamp_multiplier(multiplier_bp: Optional[int]) -> int:
    if not multiplier_bp:
        return config.DAILY_BP_START
    return max(config.DAILY_BP_FLOOR, min(int(multiplier_bp), config.DAILY_BP_CAP))


def payout_for(multiplier_bp: int) -> int:
    """Credits for a claim at this multiplier, floored to whole credits."""
    return config.DAILY_BASE * int(multiplier_bp) // 10_000


def multiplier_label(multiplier_bp: int) -> str:
    """``11000`` -> ``"110%"``."""
    return f"{int(multiplier_bp) / 100:g}%"


def evaluate(
    last_claim: Optional[dt.datetime],
    streak: int,
    multiplier_bp: Optional[int],
    now: dt.datetime,
) -> DailyOutcome:
    """Resolve a claim attempt at ``now`` against the stored daily state."""
    if last_claim is None:
        return DailyOutcome(
            eligible=True,
            multiplier_bp=config.DAILY_BP_START,
            streak=1,
            payout=payout_for(config.DAILY_BP_START),
        )

    bp = clamp_multiplier(multiplier_bp)
    streak = max(0, int(streak or 0))
    cooldown = config.DAILY_COOLDOWN_HOURS * 3_600
    elapsed = (now - last_claim).total_seconds()

    if elapsed < cooldown:
        return DailyOutcome(
            eligible=False,
            multiplier_bp=bp,
            streak=streak,
            payout=0,
            retry_in=max(1, math.ceil(cooldown - elapsed)),
        )

    days = int(elapsed // SECONDS_PER_DAY)
    if days <= 1:
        # Claimed inside the next window: the streak lives.
        new_bp = min(bp + config.DAILY_BP_STEP, config.DAILY_BP_CAP)
        new_streak = streak + 1
        missed = 0
    else:
        # Every full window past the first grace window is a missed day.
        missed = days - 1
        new_bp = max(bp - config.DAILY_BP_MISS_PENALTY * missed, config.DAILY_BP_FLOOR)
        new_streak = 1

    return DailyOutcome(
        eligible=True,
        multiplier_bp=new_bp,
        streak=new_streak,
        payout=payout_for(new_bp),
        missed_days=missed,
    )
