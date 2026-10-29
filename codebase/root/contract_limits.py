"""
contract_limits.py
==================
How large a Workers' Union contract a party may commit to, relative to the
credits it actually holds.

A nation or kingdom does not have to fund a contract in full before signing it:
the Union allows leverage that tightens as the contract gets larger.

    Contract value            Maximum contract value
    ---------------------------------------------------------------
    up to 10,000,000         3 x the party's treasury balance
    up to 20,000,000         2 x the treasury balance
    above 20,000,000         the treasury balance itself (1 x)

The bands are evaluated on the contract value. Because a larger contract drops
into a tighter band, the largest reachable contract for a given balance is the
best of the three bands, capped by ``config.CONTRACT_MAX_CREDITS``.

The minimum is ``config.CONTRACT_MIN_CREDITS`` (currently 1 credit, effectively
no practical minimum); the maximum is ``config.CONTRACT_MAX_CREDITS``
(100,000,000). The examples below are leverage caps, not eligibility.

    balance  1M  -> cap  3M   (3x, still under the 10M ceiling)
    balance  4M  -> cap 10M   (3x would be 12M, which falls in the 2x band and
                               only supports 8M, so the 10M ceiling binds)
    balance  6M  -> cap 12M   (2x)
    balance 12M  -> cap 20M   (2x up to the 20M ceiling)
    balance 30M  -> cap 30M   (1x)

Payment: the leverage ladder bounds the *party*, not the payer. Credits come
from the Workers' Union treasury, so the treasury must hold the whole contract
value before approval and is debited tranche by tranche at claim time; see
``cogs/contract.py``. The upfront tranche is at most 0.75x the party's treasury,
which keeps the party's own balance an honest signal of commitment without ever
being charged.
"""
from __future__ import annotations

import config


def leverage_for_budget(budget: int) -> int:
    """Multiplier that applies to a contract of this size."""
    for ceiling, multiplier in config.CONTRACT_LEVERAGE_TIERS:
        if ceiling is None or budget <= ceiling:
            return multiplier
    return 1


def max_budget_for_funds(funds: int) -> int:
    """Largest contract value a party holding ``funds`` credits may commit to."""
    funds = max(0, int(funds))
    best = 0
    for ceiling, multiplier in config.CONTRACT_LEVERAGE_TIERS:
        allowed = funds * multiplier
        if ceiling is not None:
            allowed = min(allowed, ceiling)
        best = max(best, allowed)
    return min(best, config.CONTRACT_MAX_CREDITS)


def budget_allowed(budget: int, funds: int) -> bool:
    """True when ``budget`` is payable under the minimum and leverage ladder."""
    return config.CONTRACT_MIN_CREDITS <= budget <= max_budget_for_funds(funds)


def limit_explanation(funds: int, *, balance_text: str) -> str:
    """Human-readable summary of the ladder for a treasury balance."""
    cap = max_budget_for_funds(funds)
    return (
        f"{balance_text} the largest contract is **{cap:,} {config.CURRENCY_NAME}** "
        f"(minimum {config.CONTRACT_MIN_CREDITS:,}; 3× funds up to 10,000,000, "
        f"2× up to 20,000,000, then 1×; absolute cap {config.CONTRACT_MAX_CREDITS:,})."
    )
