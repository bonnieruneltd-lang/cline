"""
payouts.py
==========
Pure functions that implement the Royal Court Decrees.

Nation payout (every 14 days):
    20,000
  +   250 * happy_workers
  +    30 * regular_workers
  +   200 * happy_soldiers
  +    20 * regular_soldiers

Kingdom payout (every 14 days):
    50,000
  + 10,000 * member_nations
  +    125 * happy_workers   (summed across member nations)
  +     15 * regular_workers (summed across member nations)
  +    100 * happy_soldiers  (summed across member nations)
  +     10 * regular_soldiers(summed across member nations)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

import config


@dataclass
class Breakdown:
    """A single payout line item: label, quantity, unit rate, subtotal."""
    label: str
    quantity: int
    rate: int

    @property
    def subtotal(self) -> int:
        return self.quantity * self.rate


@dataclass
class PayoutResult:
    total: int
    lines: list[Breakdown] = field(default_factory=list)


def union_dues(balance: int, account_type: str) -> int:
    """Union dues owed on a whole balance, floored to whole credits."""
    bps = config.UNION_TAX_BPS.get(account_type, 0)
    if balance <= 0 or bps <= 0:
        return 0
    return balance * bps // 10_000


def individual_tax(balance: int) -> int:
    """Net worth tax owed by one individual wallet, floored to whole credits.

    Individuals only: nations and kingdoms are not taxed by this rule.
    """
    if balance <= 0 or not config.INDIVIDUAL_TAX_ENABLED:
        return 0
    bps = config.INDIVIDUAL_TAX_BPS
    if bps <= 0:
        return 0
    return balance * bps // 10_000


def nation_payout(
    happy_workers: int,
    regular_workers: int,
    happy_soldiers: int,
    regular_soldiers: int,
) -> PayoutResult:
    lines = [
        Breakdown("Base Allowance (Rule 7.1)", 1, config.NATION_BASE),
        Breakdown("Happy Workers (Rule 7.2)", happy_workers, config.NATION_PER_HAPPY_WORKER),
        Breakdown("Regular Workers (Rule 7.3)", regular_workers, config.NATION_PER_REGULAR_WORKER),
        Breakdown("Happy Soldiers (Rule 7.4)", happy_soldiers, config.NATION_PER_HAPPY_SOLDIER),
        Breakdown("Regular Soldiers (Rule 7.5)", regular_soldiers, config.NATION_PER_REGULAR_SOLDIER),
    ]
    return PayoutResult(total=sum(l.subtotal for l in lines), lines=lines)


def kingdom_payout(member_nations: Sequence) -> PayoutResult:
    """
    `member_nations` is a sequence of rows/dicts with the four stat fields.
    Sums the stats across all member nations.
    """
    count = len(member_nations)
    hw = sum(int(n["happy_workers"]) for n in member_nations)
    rw = sum(int(n["regular_workers"]) for n in member_nations)
    hs = sum(int(n["happy_soldiers"]) for n in member_nations)
    rs = sum(int(n["regular_soldiers"]) for n in member_nations)

    lines = [
        Breakdown("Base Allowance (Rule 8.1)", 1, config.KINGDOM_BASE),
        Breakdown("Member Nations (Rule 8.2)", count, config.KINGDOM_PER_MEMBER_NATION),
        Breakdown("Happy Workers (Rule 8.3)", hw, config.KINGDOM_PER_HAPPY_WORKER),
        Breakdown("Regular Workers (Rule 8.4)", rw, config.KINGDOM_PER_REGULAR_WORKER),
        Breakdown("Happy Soldiers (Rule 8.5)", hs, config.KINGDOM_PER_HAPPY_SOLDIER),
        Breakdown("Regular Soldiers (Rule 8.6)", rs, config.KINGDOM_PER_REGULAR_SOLDIER),
    ]
    return PayoutResult(total=sum(l.subtotal for l in lines), lines=lines)
