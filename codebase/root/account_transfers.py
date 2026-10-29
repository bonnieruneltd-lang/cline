"""
account_transfers.py
====================
One atomic credit-transfer path for every economy account.

The Royal Court has three economy account kinds:

* ``nation`` and ``kingdom`` treasuries (``nations`` / ``kingdoms`` rows)
* ``individual`` wallets (``individuals`` rows, one per member per guild)

Historically each direction had its own command and its own SQL. This module is
the single implementation behind them, so every transfer:

1. is resolved inside the guild that owns the account (no cross-guild moves);
2. re-reads both accounts under the database transaction lock;
3. requires the actor to lead the source treasury or own the source wallet
   (administrators may override);
4. debits the source with the balance guard in the ``UPDATE``;
5. credits the destination, using the strict individual credit so a transfer
   into a capped wallet is refused instead of silently trimmed;
6. writes both ledger rows in the same transaction; and
7. runs the destination treasury's standing Union commitment hook, if active.

Only credits move. The bot does not model item/asset ownership on an account,
so "asset transfer" in this codebase means credits; shop orders and kits are
per-buyer records, not account-held assets.

Explicit inbound transfers create a missing individual wallet (the recipient is
being credited, exactly like ``/individual transfer``). Settlement paths still
never recreate a wallet that vanished mid-hand; that rule lives in
``casino.finish_wager`` and ``credit_individual_strict``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from database import Database

NATION = "nation"
KINGDOM = "kingdom"
INDIVIDUAL = "individual"
ACCOUNT_KINDS = (NATION, KINGDOM, INDIVIDUAL)

_TABLES = {NATION: "nations", KINGDOM: "kingdoms"}


class TransferError(Exception):
    """A user-facing refusal; no credits moved."""


@dataclass(frozen=True)
class AccountRef:
    """A resolved account, independent of the Discord interaction that chose it.

    ``id`` is the row id used in the ledger (the wallet id for individuals).
    ``discord_id`` is the member id for individuals; ``leader_id`` is the
    treasury leader for nations/kingdoms.
    """

    kind: str
    id: int
    name: str
    balance: int
    leader_id: Optional[int] = None
    discord_id: Optional[int] = None

    @property
    def key(self) -> Tuple[str, int]:
        """Identity for same-account checks; survives wallet recreation."""
        if self.kind == INDIVIDUAL:
            return (self.kind, int(self.discord_id or 0))
        return (self.kind, int(self.id))


@dataclass(frozen=True)
class TransferResult:
    """What actually moved, and both balances after the transaction."""

    amount: int
    source: AccountRef
    destination: AccountRef
    source_balance: int
    destination_balance: int
    committed: int = 0


# ---------------------------------------------------------------------------
# Account references
# ---------------------------------------------------------------------------
def encode_ref(kind: str, key) -> str:
    """Encode an account for an app-command option / autocomplete value."""
    if kind not in ACCOUNT_KINDS:
        raise ValueError(f"unknown account kind {kind!r}")
    return f"{kind}:{key}"


def parse_ref(value: str) -> Tuple[Optional[str], Optional[str]]:
    """Split ``nation:Name``, ``kingdom:Name`` or ``individual:<discord_id>``."""
    if not value or ":" not in value:
        return None, None
    kind, _, key = value.partition(":")
    kind = kind.strip().lower()
    key = key.strip()
    if kind not in ACCOUNT_KINDS or not key:
        return None, None
    return kind, key


def _discord_id(value: str) -> Optional[int]:
    """Accept a bare id, ``<@id>`` or ``<@!id>``; reject anything else."""
    text = (value or "").strip()
    if text.startswith("<@") and text.endswith(">"):
        text = text[2:-1].lstrip("!&")
    return int(text) if text.isdigit() else None


def _individual_name(row) -> str:
    return row["display_name"] or f"<@{row['discord_id']}>"


def _individual_ref(row) -> AccountRef:
    return AccountRef(
        kind=INDIVIDUAL,
        id=int(row["id"]),
        name=_individual_name(row),
        balance=int(row["balance"]),
        discord_id=int(row["discord_id"]),
    )


def _treasury_ref(kind: str, row) -> AccountRef:
    return AccountRef(
        kind=kind,
        id=int(row["id"]),
        name=row["name"],
        balance=int(row["balance"]),
        leader_id=row["leader_id"],
    )


async def resolve_ref(
    db: Database, guild_id: int, value: str
) -> Optional[AccountRef]:
    """Resolve one autocomplete value to a live account in ``guild_id``."""
    kind, key = parse_ref(value)
    if kind is None:
        return None
    if kind == NATION:
        row = await db.get_nation(guild_id, key)
        return _treasury_ref(NATION, row) if row else None
    if kind == KINGDOM:
        row = await db.get_kingdom(guild_id, key)
        return _treasury_ref(KINGDOM, row) if row else None

    discord_id = _discord_id(key)
    if discord_id is None:
        return None
    row = await db.find_individual(guild_id, discord_id)
    return _individual_ref(row) if row else None


def ref_label(ref: AccountRef) -> str:
    """Human-facing label used in replies, ledger notes and errors."""
    emoji = {NATION: "🏛️", KINGDOM: "👑", INDIVIDUAL: "👤"}[ref.kind]
    label = {NATION: "Nation", KINGDOM: "Kingdom", INDIVIDUAL: "Member"}[ref.kind]
    return f"{emoji} {label} · {ref.name}"


def may_spend(actor_id: int, is_admin: bool, ref: AccountRef) -> bool:
    """Whether ``actor_id`` may move credits out of ``ref``."""
    if is_admin:
        return True
    if ref.kind == INDIVIDUAL:
        return ref.discord_id is not None and int(ref.discord_id) == int(actor_id)
    return ref.leader_id is not None and int(ref.leader_id) == int(actor_id)


def same_account(left: AccountRef, right: AccountRef) -> bool:
    return left.key == right.key



# ---------------------------------------------------------------------------
# Fresh reads inside the transaction
# ---------------------------------------------------------------------------
async def _reload_source(
    db: Database, guild_id: int, ref: AccountRef
) -> Optional[AccountRef]:
    if ref.kind == INDIVIDUAL:
        if not ref.discord_id:
            return None
        row = await db.find_individual(guild_id, int(ref.discord_id))
        return _individual_ref(row) if row else None

    row = (
        await db.get_kingdom_by_id(ref.id)
        if ref.kind == KINGDOM
        else await db.get_nation_by_id(ref.id)
    )
    if row is None or int(row["guild_id"]) != int(guild_id):
        return None
    return _treasury_ref(ref.kind, row)


async def _reload_destination(
    db: Database, guild_id: int, ref: AccountRef
) -> Optional[AccountRef]:
    if ref.kind == INDIVIDUAL:
        if not ref.discord_id:
            return None
        # A transfer *into* a wallet may create it on first receipt. Spending
        # from a missing wallet is refused by _reload_source, so a deleted
        # source is never resurrected.
        display_name = None if ref.name.startswith("<@") else ref.name
        row = await db.get_individual(guild_id, int(ref.discord_id), display_name)
        return _individual_ref(row)

    row = (
        await db.get_kingdom_by_id(ref.id)
        if ref.kind == KINGDOM
        else await db.get_nation_by_id(ref.id)
    )
    if row is None or int(row["guild_id"]) != int(guild_id):
        return None
    return _treasury_ref(ref.kind, row)


# ---------------------------------------------------------------------------
# The one transfer implementation
# ---------------------------------------------------------------------------
async def transfer_funds(
    db: Database,
    *,
    guild_id: int,
    actor_id: int,
    is_admin: bool,
    source: AccountRef,
    destination: AccountRef,
    amount: int,
    kind: str = "transfer",
    note: str = "",
) -> TransferResult:
    """Move ``amount`` credits between any two economy accounts atomically.

    Raises :class:`TransferError` for a validation/permission refusal,
    ``InsufficientFunds`` when the source cannot pay, ``CapExceeded`` when an
    individual destination cannot hold the credit, and ``AccountMissing`` when
    a treasury vanishes mid-flight. Any exception rolls the whole movement
    back, including both ledger rows and a Union commitment.
    """
    amount = int(amount)
    if amount <= 0:
        raise TransferError("The amount must be greater than zero.")
    if source.kind not in ACCOUNT_KINDS or destination.kind not in ACCOUNT_KINDS:
        raise TransferError("That account type cannot be used in a transfer.")

    async with db.transaction():
        # Re-read both sides under BEGIN IMMEDIATE: the command's autocomplete
        # snapshot is never the authority for balances or leadership.
        fresh_source = await _reload_source(db, guild_id, source)
        if fresh_source is None:
            raise TransferError("The source account no longer exists in this server.")
        if not may_spend(actor_id, is_admin, fresh_source):
            raise TransferError(
                "You can only move credits out of a treasury you lead, your own "
                "wallet, or as an administrator."
            )

        fresh_destination = await _reload_destination(db, guild_id, destination)
        if fresh_destination is None:
            raise TransferError("The destination account no longer exists in this server.")
        if same_account(fresh_source, fresh_destination):
            raise TransferError("Source and destination are the same account.")

        if fresh_source.kind == INDIVIDUAL:
            source_after = await db.debit_individual(
                guild_id, int(fresh_source.discord_id), amount
            )
        else:
            source_after = await db.debit(
                _TABLES[fresh_source.kind], fresh_source.id, amount
            )

        if fresh_destination.kind == INDIVIDUAL:
            _added, destination_after = await db.credit_individual_strict(
                guild_id,
                int(fresh_destination.discord_id),
                amount,
                display_name=(
                    None
                    if fresh_destination.name.startswith("<@")
                    else fresh_destination.name
                ),
            )
        else:
            destination_after = await db.add_balance(
                _TABLES[fresh_destination.kind], fresh_destination.id, amount
            )

        await db.log_tx(
            guild_id=guild_id,
            kind=kind,
            account_type=fresh_source.kind,
            account_id=fresh_source.id,
            account_name=fresh_source.name,
            counterparty_type=fresh_destination.kind,
            counterparty_id=fresh_destination.id,
            counterparty_name=fresh_destination.name,
            amount=-amount,
            balance_after=source_after,
            actor_id=actor_id,
            note=note or f"Transfer to {ref_label(fresh_destination)}",
        )
        await db.log_tx(
            guild_id=guild_id,
            kind=kind,
            account_type=fresh_destination.kind,
            account_id=fresh_destination.id,
            account_name=fresh_destination.name,
            counterparty_type=fresh_source.kind,
            counterparty_id=fresh_source.id,
            counterparty_name=fresh_source.name,
            amount=amount,
            balance_after=destination_after,
            actor_id=actor_id,
            note=note or f"Transfer from {ref_label(fresh_source)}",
        )

        committed = 0
        if fresh_destination.kind != INDIVIDUAL:
            # A standing Union commitment donates the destination's whole
            # balance on every credit; the hook belongs in this transaction so
            # the transfer cannot land without it.
            committed = await db.commit_balance_to_union(
                guild_id, fresh_destination.kind, fresh_destination.id
            )
            if committed:
                destination_after -= committed

    return TransferResult(
        amount=amount,
        source=fresh_source,
        destination=fresh_destination,
        source_balance=int(source_after),
        destination_balance=int(destination_after),
        committed=int(committed),
    )

