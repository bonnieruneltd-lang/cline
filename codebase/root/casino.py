"""
casino.py
=========
Settlement against the Casino house account.

Every wager in the realm is **zero-sum against the house**: the stake moves from
the player's wallet into the Casino, and a win is paid back out of it. Nothing
creates credits. That is the whole point of the account - before it existed, a
coinflip win minted the stake out of nothing and only the 49/51 split kept the
supply in check. Now a win is funded by previous losses, so the money supply can
only grow when an admin funds the house with ``/casino fund``.

The consequence is that a bet must be **refused when the house cannot cover the
payout**. Otherwise a lucky player would be owed credits the Casino does not
have, and paying them would mean printing money again.

Two-step settlement
-------------------
Long games (blackjack and poker) need the stake held while the hand is in play,
so the player cannot spend it mid-hand and then claim a loss cost them nothing.
:func:`begin_wager` takes the stake, :func:`finish_wager` pays the result. A
one-shot game (coinflip, all-in) calls both inside a single transaction, so it
settles atomically anyway. Poker uses ``payout_override`` because its payout is
the remaining table stack, not a simple win/push/loss amount.

Both steps write their own ledger rows: the hold is a real movement and the
append-only log has to show it even if the process dies mid-hand.
"""

from __future__ import annotations

from typing import Optional, Tuple

import config
from database import AccountMissing, Database


def house_can_cover(house: int, gross: int) -> bool:
    """Whether the Casino can pay ``gross`` out of ``house``."""
    return int(house) >= int(gross)


def cover_problem(house: int, gross: int) -> Optional[str]:
    """Why the house cannot take this bet, or ``None`` when it can.

    ``gross`` is the *total* return on a win (stake included), because that is
    what has to leave the house balance.
    """
    house = int(house)
    gross = int(gross)
    if gross <= house:
        return None
    return (
        f"The Casino cannot cover the **{gross:,}** credit payout on that bet "
        f"(house holds {house:,}). Take a smaller stake or a smaller multiplier."
    )


def wager_problem(
    *, stake: int, balance: int, gross: int, house: int, minimum: int
) -> Optional[str]:
    """Every reason this wager cannot be placed, checked before any money moves."""
    stake = int(stake)
    if stake < minimum:
        return f"The minimum bet is **{minimum:,}** credits."
    if stake > balance:
        return "You do not hold that much in your individual balance."
    room = config.INDIVIDUAL_MAX_CREDITS - int(balance) + stake
    if gross > room:
        return (
            f"A win would return **{gross:,}** and take you past the individual cap of "
            f"**{config.INDIVIDUAL_MAX_CREDITS:,}** credits. Bet less."
        )
    return cover_problem(house, gross)


async def begin_wager(
    db: Database,
    *,
    guild_id: int,
    wallet,
    stake: int,
    kind: str,
    actor_id: Optional[int] = None,
    note: str = "",
) -> Tuple[int, int]:
    """Move the stake from the player's wallet into the Casino.

    Returns ``(player_balance, house_balance)``. Raises
    :class:`InsufficientFunds` if the wallet no longer covers the stake and
    :class:`AccountMissing` if the wallet was erased.
    """
    # Re-read the wallet under the caller's transaction. ``wallet`` was read
    # before the hand opened, so using its id for the ledger after a delete and
    # recreate would point at the dead row. Spending must not create a wallet.
    fresh = await db.find_individual(guild_id, int(wallet["discord_id"]))
    if fresh is None:
        raise AccountMissing("individuals", int(wallet["discord_id"]))
    wallet = fresh
    label = wallet["display_name"] or str(wallet["discord_id"])
    player_after = await db.debit_individual(guild_id, wallet["discord_id"], stake)
    house_after = await db.credit_casino(guild_id, stake)

    await db.log_tx(
        guild_id=guild_id,
        kind=kind,
        account_type="individual",
        account_id=wallet["id"],
        account_name=label,
        counterparty_type="casino",
        counterparty_name="The Casino",
        amount=-int(stake),
        balance_after=player_after,
        actor_id=actor_id,
        note=note or "Stake",
    )
    await db.log_tx(
        guild_id=guild_id,
        kind=kind,
        account_type="casino",
        account_id=guild_id,
        account_name="The Casino",
        counterparty_type="individual",
        counterparty_id=wallet["id"],
        counterparty_name=label,
        amount=int(stake),
        balance_after=house_after,
        actor_id=actor_id,
        note=note or f"Stake from {label}",
    )
    return player_after, house_after


async def finish_wager(
    db: Database,
    *,
    guild_id: int,
    wallet,
    stake: int,
    gross: int,
    outcome: str,
    kind: str,
    actor_id: Optional[int] = None,
    note: str = "",
    payout_override: Optional[int] = None,
) -> Tuple[int, int]:
    """Pay out a settled wager. ``outcome`` is ``win``, ``loss`` or ``push``.

    A win pays ``gross`` from the house, a push returns the stake, and a loss
    pays nothing (the house already has the stake from :func:`begin_wager`).
    ``payout_override`` is for games whose payout is not one of those three
    shapes: poker returns the player's *remaining table stack*, which can be
    positive even on a losing hand. Returns ``(player_balance, house_balance)``;
    both are re-read even on a loss so callers can display them.
    """
    # Settle against the wallet as it exists now, never the pre-hand snapshot.
    # A deleted wallet is not recreated: a payout raises AccountMissing so the
    # caller's transaction rolls back and the hand stays open for the delete
    # guard/startup refund. A loss on a deleted wallet simply moves nothing.
    payout = int(gross) if outcome == "win" else int(stake) if outcome == "push" else 0
    if payout_override is not None:
        payout = max(0, int(payout_override))
    fresh = await db.find_individual(guild_id, int(wallet["discord_id"]))
    if fresh is None:
        if payout:
            raise AccountMissing("individuals", int(wallet["discord_id"]))
        return 0, await db.get_casino_balance(guild_id)
    wallet = fresh
    label = wallet["display_name"] or str(wallet["discord_id"])

    if payout:
        house_after = await db.debit_casino(guild_id, payout)
        # credit_individual_strict returns (amount_added, balance_after).
        _added, player_after = await db.credit_individual_strict(
            guild_id, wallet["discord_id"], payout, display_name=wallet["display_name"]
        )
        await db.log_tx(
            guild_id=guild_id,
            kind=kind,
            account_type="individual",
            account_id=wallet["id"],
            account_name=label,
            counterparty_type="casino",
            counterparty_name="The Casino",
            amount=payout,
            balance_after=player_after,
            actor_id=actor_id,
            note=note or ("Winnings" if outcome == "win" else "Stake returned"),
        )
        await db.log_tx(
            guild_id=guild_id,
            kind=kind,
            account_type="casino",
            account_id=guild_id,
            account_name="The Casino",
            counterparty_type="individual",
            counterparty_id=wallet["id"],
            counterparty_name=label,
            amount=-payout,
            balance_after=house_after,
            actor_id=actor_id,
            note=note or f"Payout to {label}",
        )
        return player_after, house_after

    row = await db.find_individual(guild_id, wallet["discord_id"])
    return int(row["balance"]) if row else 0, await db.get_casino_balance(guild_id)


def net_change(*, stake: int, gross: int, outcome: str) -> int:
    """Player's net movement across a settled wager, for display and tests."""
    if outcome == "win":
        return int(gross) - int(stake)
    if outcome == "push":
        return 0
    return -int(stake)
